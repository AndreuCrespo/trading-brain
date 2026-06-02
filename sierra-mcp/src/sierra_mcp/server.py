import asyncio
import logging
import os
import sys
import time
from datetime import datetime, time as dt_time, timedelta, timezone
from pathlib import Path

# When loaded by `mcp dev` (which imports this file by path, not as a package),
# ensure the parent `src/` is on sys.path so the sierra_mcp.* imports resolve.
_src_dir = Path(__file__).resolve().parents[1]
if str(_src_dir) not in sys.path:
    sys.path.insert(0, str(_src_dir))

from mcp.server.fastmcp import FastMCP

from sierra_mcp.config import Config
from sierra_mcp.dtc_client import DTCClient
from sierra_mcp.dtc_messages import MessageType
from sierra_mcp import scid_reader

logging.basicConfig(level=logging.INFO)

mcp = FastMCP("sierra-mcp")


MARKET_DATA_REQUEST_SUBSCRIBE = 1
MARKET_DATA_REQUEST_UNSUBSCRIBE = 2
MARKET_DATA_REQUEST_SNAPSHOT = 3

INTERVAL_SECONDS = {
    "tick": 0,
    "1s": 1,
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
    "4h": 14400,
    "1d": 86400,
}

SESSION_LOOKBACK_RECORDS = 1_000_000


def datetime_from_unix(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _atr(bars: list[dict], period: int) -> float | None:
    if len(bars) < 2:
        return None

    true_ranges: list[float] = []
    for i in range(1, len(bars)):
        high = float(bars[i]["high"])
        low = float(bars[i]["low"])
        prev_close = float(bars[i - 1]["close"])
        true_ranges.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))

    if not true_ranges:
        return None
    sample = true_ranges[-period:]
    return round(sum(sample) / len(sample), 4)


def _simple_trend(bars: list[dict]) -> str:
    if len(bars) < 5:
        return "unknown"

    closes = [float(b["close"]) for b in bars]
    short = closes[-5:]
    slope = short[-1] - short[0]
    higher_high = float(bars[-1]["high"]) >= max(float(b["high"]) for b in bars[-5:-1])
    lower_low = float(bars[-1]["low"]) <= min(float(b["low"]) for b in bars[-5:-1])

    if slope > 0 and higher_high:
        return "up"
    if slope < 0 and lower_low:
        return "down"
    return "sideways"


def _delta_percent(bid_volume: int | float, ask_volume: int | float) -> float | None:
    total = bid_volume + ask_volume
    if total <= 0:
        return None
    return round((ask_volume - bid_volume) / total * 100, 2)


def _globex_equity_session_start(last_tick_unix: float) -> float:
    """Approximate CME equity index futures session start in UTC.

    ES/NQ/MES/MNQ trade nearly 24h with the main daily Globex session starting
    at 17:00 Chicago, which is 22:00 UTC during US daylight saving time. This is
    good enough for the current CME futures context MVP.
    """
    dt = datetime.fromtimestamp(last_tick_unix, tz=timezone.utc)
    session_date = dt.date()
    if dt.timetz() < dt_time(22, 0, tzinfo=timezone.utc):
        session_date = session_date - timedelta(days=1)
    session_start = datetime.combine(session_date, dt_time(22, 0, tzinfo=timezone.utc))
    return session_start.timestamp()


@mcp.tool()
async def ping_sierra() -> dict:
    """Connect to Sierra Chart via DTC, perform logon, and return the logon response.

    Use this to verify the bridge is alive and the credentials work before invoking
    any data or trading tools.
    """
    config = Config.from_env()
    client = DTCClient(config.host, config.trading_port, config)
    try:
        response = await client.connect()
        return {
            "ok": True,
            "server": response.get("ServerName"),
            "server_version": response.get("ServerVersion"),
            "result_text": response.get("ResultText"),
            "trading_supported": bool(response.get("TradingIsSupported")),
            "market_data_supported": bool(response.get("MarketDataSupported")),
            "reconnect_address": response.get("ReconnectAddress"),
        }
    finally:
        await client.close()


@mcp.tool()
async def get_quote(symbol: str, exchange: str = "CME") -> dict:
    """Get a real-time market data snapshot for a symbol.

    symbol: Sierra Chart contract symbol. Examples for current front-month futures:
            "ESM26-CME" (E-mini S&P 500 Jun 2026), "NQM26-CME" (E-mini Nasdaq Jun 2026),
            "MESM26-CME" (Micro E-mini S&P), "MNQM26-CME" (Micro E-mini Nasdaq).
            Continuous contracts use a '#' suffix: "ES#-CME".
    exchange: Exchange code (e.g. "CME", "CBOT", "NYMEX"). Defaults to "CME".

    Returns last/bid/ask, session OHLC, volume and open interest. Outside RTH or
    on closed markets the values reflect the most recent session.
    """
    config = Config.from_env()
    client = DTCClient(config.host, config.trading_port, config)
    try:
        await client.connect()
        try:
            response = await client.request(
                {
                    "Type": int(MessageType.MARKET_DATA_REQUEST),
                    "RequestAction": MARKET_DATA_REQUEST_SUBSCRIBE,
                    "SymbolID": 1,
                    "Symbol": symbol,
                    "Exchange": exchange,
                },
                [MessageType.MARKET_DATA_SNAPSHOT, MessageType.MARKET_DATA_REJECT],
                timeout=10,
            )

            if response.get("Type") == MessageType.MARKET_DATA_REJECT:
                return {
                    "ok": False,
                    "symbol": symbol,
                    "reject_reason": response.get("RejectText"),
                }
        finally:
            try:
                await client.send({
                    "Type": int(MessageType.MARKET_DATA_REQUEST),
                    "RequestAction": MARKET_DATA_REQUEST_UNSUBSCRIBE,
                    "SymbolID": 1,
                    "Symbol": symbol,
                    "Exchange": exchange,
                })
            except Exception:
                pass

        return {
            "ok": True,
            "symbol": symbol,
            "exchange": exchange,
            "bid": response.get("BidPrice"),
            "ask": response.get("AskPrice"),
            "bid_size": response.get("BidQuantity"),
            "ask_size": response.get("AskQuantity"),
            "last_price": response.get("LastTradePrice"),
            "last_size": response.get("LastTradeVolume"),
            "last_time": response.get("LastTradeDateTime"),
            "session_open": response.get("SessionOpenPrice"),
            "session_high": response.get("SessionHighPrice"),
            "session_low": response.get("SessionLowPrice"),
            "session_settlement": response.get("SessionSettlementPrice"),
            "session_volume": response.get("SessionVolume"),
            "session_num_trades": response.get("SessionNumTrades"),
            "open_interest": response.get("OpenInterest"),
        }
    finally:
        await client.close()


@mcp.tool()
async def get_recent_bars(
    symbol: str,
    interval: str = "1m",
    count: int = 100,
    exchange: str = "CME",
) -> dict:
    """Get recent historical OHLCV bars for a symbol from Sierra's historical data port.

    Use this when real-time market data is unavailable via DTC (e.g. the SC Data
    feed restricts redistribution). The last bar's close approximates the most
    recent known price.

    symbol: Contract symbol, e.g. "MESM26-CME", "NQM26-CME".
    interval: One of "tick", "1s", "1m", "5m", "15m", "30m", "1h", "4h", "1d".
    count: Number of most recent bars to return (default 100, hard cap 5000).
    exchange: Exchange code, default "CME".
    """
    if interval not in INTERVAL_SECONDS:
        return {"ok": False, "error": f"interval must be one of {list(INTERVAL_SECONDS)}"}
    count = max(1, min(count, 5000))

    seconds = INTERVAL_SECONDS[interval]
    if seconds > 0:
        start_ts = int(time.time() - count * seconds * 3)
    else:
        start_ts = int(time.time() - 3600)

    config = Config.from_env()
    client = DTCClient(config.host, config.historical_port, config)
    try:
        await client.connect()

        record_q = client.subscribe(MessageType.HISTORICAL_PRICE_DATA_RECORD)
        try:
            header = await client.request(
                {
                    "Type": int(MessageType.HISTORICAL_PRICE_DATA_REQUEST),
                    "RequestID": 1,
                    "Symbol": symbol,
                    "Exchange": exchange,
                    "RecordInterval": seconds,
                    "StartDateTime": start_ts,
                    "EndDateTime": 0,
                    "MaxDaysToReturn": 0,
                    "UseZLibCompression": 0,
                    "RequestDividendAdjustedStockData": 0,
                    "Flag_1": 0,
                },
                [
                    MessageType.HISTORICAL_PRICE_DATA_HEADER,
                    MessageType.HISTORICAL_PRICE_DATA_REJECT,
                ],
                timeout=15,
            )

            if header.get("Type") == MessageType.HISTORICAL_PRICE_DATA_REJECT:
                return {
                    "ok": False,
                    "symbol": symbol,
                    "reject_reason": header.get("RejectText"),
                }

            if header.get("NoRecordsToReturn"):
                return {"ok": True, "symbol": symbol, "interval": interval, "bars": []}

            bars: list[dict] = []
            while True:
                try:
                    rec = await asyncio.wait_for(record_q.get(), timeout=30)
                except asyncio.TimeoutError:
                    break
                bars.append({
                    "time": rec.get("StartDateTime"),
                    "open": rec.get("OpenPrice"),
                    "high": rec.get("HighPrice"),
                    "low": rec.get("LowPrice"),
                    "close": rec.get("LastPrice"),
                    "volume": rec.get("Volume"),
                    "num_trades": rec.get("NumTrades"),
                    "bid_volume": rec.get("BidVolume"),
                    "ask_volume": rec.get("AskVolume"),
                })
                if rec.get("IsFinalRecord"):
                    break

            bars = bars[-count:]
            return {
                "ok": True,
                "symbol": symbol,
                "exchange": exchange,
                "interval": interval,
                "count": len(bars),
                "bars": bars,
            }
        finally:
            client.unsubscribe(MessageType.HISTORICAL_PRICE_DATA_RECORD)
    finally:
        await client.close()


@mcp.tool()
async def get_recent_bars_scid(
    symbol: str,
    interval: str = "1m",
    count: int = 100,
) -> dict:
    """Read recent OHLCV bars by parsing Sierra Chart's local .scid tick file.

    Works without DTC market data permission — reads the local file Sierra writes.
    Lags real-time by however often Sierra flushes ticks to disk (usually seconds).

    symbol: e.g. "MESM26-CME". A file <symbol>.scid must exist in the data dir.
    interval: One of "1m", "5m", "15m", "30m", "1h", "4h", "1d".
    count: Number of most recent bars to return (default 100, cap 5000).
    """
    if interval not in INTERVAL_SECONDS or interval in ("tick", "1s"):
        return {"ok": False, "error": "interval must be 1m/5m/15m/30m/1h/4h/1d"}

    interval_sec = INTERVAL_SECONDS[interval]
    count = max(1, min(count, 5000))

    config = Config.from_env()
    scid_path = os.path.join(config.data_path, f"{symbol}.scid")
    if not os.path.exists(scid_path):
        return {"ok": False, "error": f"file not found: {scid_path}"}

    # Read ~10x the records we'd theoretically need (assuming ~10 ticks/sec average
    # across market + off-hours), capped at 1M records (40 MB).
    estimate = count * interval_sec * 10
    max_records = max(50_000, min(estimate, 1_000_000))

    records = await asyncio.to_thread(scid_reader.read_tail_records, scid_path, max_records)
    bars = scid_reader.aggregate_to_bars(records, interval_sec)
    bars = bars[-count:]

    return {
        "ok": True,
        "symbol": symbol,
        "interval": interval,
        "count": len(bars),
        "source": scid_path,
        "ticks_scanned": len(records),
        "bars": bars,
    }


@mcp.tool()
async def get_latest_tick_scid(symbol: str) -> dict:
    """Read the latest tick/record from Sierra Chart's local .scid file.

    This is the practical near-real-time path when Sierra's DTC market data is
    blocked by exchange restrictions. It returns the last trade price and file
    freshness. The delay is Sierra's file flush cadence, usually seconds.
    """
    config = Config.from_env()
    scid_path = os.path.join(config.data_path, f"{symbol}.scid")
    if not os.path.exists(scid_path):
        return {"ok": False, "error": f"file not found: {scid_path}"}

    record = await asyncio.to_thread(scid_reader.read_last_record, scid_path)
    status = await asyncio.to_thread(scid_reader.file_status, scid_path)
    if record is None:
        return {"ok": False, "error": f"no records in file: {scid_path}"}

    now = time.time()
    tick_age_seconds = max(0.0, now - record.unix_time)
    file_age_seconds = max(0.0, now - status["modified_unix"])
    return {
        "ok": True,
        "symbol": symbol,
        "source": scid_path,
        "price": record.close,
        "time": datetime_from_unix(record.unix_time),
        "volume": record.volume,
        "num_trades": record.num_trades,
        "bid_volume": record.bid_volume,
        "ask_volume": record.ask_volume,
        "tick_age_seconds": round(tick_age_seconds, 3),
        "file_age_seconds": round(file_age_seconds, 3),
        "record_count": status["record_count"],
        "file_modified_time": status["modified_time"],
    }


@mcp.tool()
async def get_scid_status(symbol: str) -> dict:
    """Check whether Sierra's local .scid file for a symbol is being updated."""
    config = Config.from_env()
    scid_path = os.path.join(config.data_path, f"{symbol}.scid")
    if not os.path.exists(scid_path):
        return {"ok": False, "error": f"file not found: {scid_path}"}

    record = await asyncio.to_thread(scid_reader.read_last_record, scid_path)
    status = await asyncio.to_thread(scid_reader.file_status, scid_path)
    now = time.time()
    out = {
        "ok": True,
        "symbol": symbol,
        **status,
        "file_age_seconds": round(max(0.0, now - status["modified_unix"]), 3),
    }
    if record is not None:
        out.update({
            "last_tick_time": datetime_from_unix(record.unix_time),
            "last_price": record.close,
            "tick_age_seconds": round(max(0.0, now - record.unix_time), 3),
        })
    return out


@mcp.tool()
async def get_futures_context(
    symbol: str,
    interval: str = "1m",
    bars_count: int = 30,
    atr_period: int = 14,
) -> dict:
    """Return a compact futures context pack from Sierra's local .scid file.

    Designed for ES/NQ/MES/MNQ analysis when DTC market data is unavailable:
    latest tick, file freshness, recent bars, session OHLC/VWAP/delta, ATR and
    a simple directional read. This is the main market-context tool for Claude
    conversations.
    """
    if interval not in INTERVAL_SECONDS or interval in ("tick", "1s"):
        return {"ok": False, "error": "interval must be 1m/5m/15m/30m/1h/4h/1d"}

    bars_count = max(5, min(bars_count, 500))
    atr_period = max(2, min(atr_period, 100))
    interval_sec = INTERVAL_SECONDS[interval]

    config = Config.from_env()
    scid_path = os.path.join(config.data_path, f"{symbol}.scid")
    if not os.path.exists(scid_path):
        return {"ok": False, "error": f"file not found: {scid_path}"}

    records = await asyncio.to_thread(
        scid_reader.read_tail_records,
        scid_path,
        SESSION_LOOKBACK_RECORDS,
    )
    if not records:
        return {"ok": False, "error": f"no records in file: {scid_path}"}

    bars = scid_reader.aggregate_to_bars(records, interval_sec)
    recent_bars = bars[-bars_count:]
    session_start = _globex_equity_session_start(records[-1].unix_time)
    session_records = [r for r in records if r.unix_time >= session_start]
    if not session_records:
        session_records = records
    session = scid_reader.aggregate_session_stats(session_records)
    status = await asyncio.to_thread(scid_reader.file_status, scid_path)
    last_record = records[-1]

    last_price = last_record.close
    vwap = session.get("vwap")
    distance_to_vwap = (last_price - vwap) if vwap is not None else None
    distance_to_vwap_points = round(distance_to_vwap, 4) if distance_to_vwap is not None else None

    atr = _atr(recent_bars, atr_period)
    trend = _simple_trend(recent_bars)
    now = time.time()
    tick_age = max(0.0, now - last_record.unix_time)
    file_age = max(0.0, now - status["modified_unix"])

    warnings: list[str] = []
    if file_age > 10:
        warnings.append(f"SCID file is stale by {file_age:.1f}s")
    if tick_age > 30:
        warnings.append(f"last tick is stale by {tick_age:.1f}s")
    if len(recent_bars) < bars_count:
        warnings.append(f"only {len(recent_bars)} bars available")

    bias_parts: list[str] = []
    if distance_to_vwap is not None:
        bias_parts.append("above_vwap" if distance_to_vwap > 0 else "below_vwap")
    bias_parts.append(trend)

    return {
        "ok": True,
        "symbol": symbol,
        "interval": interval,
        "source": scid_path,
        "latest": {
            "price": last_price,
            "time": datetime_from_unix(last_record.unix_time),
            "tick_age_seconds": round(tick_age, 3),
            "file_age_seconds": round(file_age, 3),
            "volume": last_record.volume,
            "bid_volume": last_record.bid_volume,
            "ask_volume": last_record.ask_volume,
        },
        "session": session,
        "indicators": {
            "vwap": vwap,
            "distance_to_vwap_points": distance_to_vwap_points,
            "atr": atr,
            "atr_period": atr_period,
            "delta": session.get("delta"),
            "delta_percent": _delta_percent(session.get("bid_volume", 0), session.get("ask_volume", 0)),
        },
        "read": {
            "trend": trend,
            "bias": ", ".join(bias_parts),
            "warnings": warnings,
        },
        "bars_count": len(recent_bars),
        "bars": recent_bars,
        "ticks_scanned": len(records),
        "session_ticks": len(session_records),
        "file": {
            "record_count": status["record_count"],
            "modified_time": status["modified_time"],
        },
    }


async def _collect_multi(
    client: DTCClient,
    request_msg: dict,
    response_type: int,
    reject_type: int,
    no_items_key: str,
    timeout: float = 10,
) -> tuple[bool, list[dict] | str]:
    """Send a request and collect a multi-message response.

    Returns (ok, messages_or_reject_text). Handles three response shapes:
      - Reject message → returns (False, reject_text)
      - Single response with NoX flag set → returns (True, [])
      - N messages with TotalNumberMessages/MessageNumber → returns (True, [msgs])
    """
    response_q = client.subscribe(response_type)
    reject_q = client.subscribe(reject_type)
    try:
        await client.send(request_msg)

        response_task = asyncio.create_task(response_q.get())
        reject_task = asyncio.create_task(reject_q.get())
        done, pending = await asyncio.wait(
            [response_task, reject_task],
            timeout=timeout,
            return_when=asyncio.FIRST_COMPLETED,
        )
        for t in pending:
            t.cancel()
        if not done:
            raise asyncio.TimeoutError("no response")
        first = done.pop().result()

        if first.get("Type") == reject_type:
            return False, first.get("RejectText", "rejected")

        if first.get(no_items_key):
            return True, []

        messages = [first]
        total = int(first.get("TotalNumberMessages", 1) or 1)
        while len(messages) < total:
            msg = await asyncio.wait_for(response_q.get(), timeout=timeout)
            messages.append(msg)
        return True, messages
    finally:
        client.unsubscribe(response_type)
        client.unsubscribe(reject_type)


@mcp.tool()
async def list_trade_accounts() -> dict:
    """List trade accounts available on the connected Sierra Chart session.

    Useful before calling get_account_balance / get_positions to know which
    TradeAccount strings to pass (Teton accounts look like USERNAME-Sim or similar).
    """
    config = Config.from_env()
    client = DTCClient(config.host, config.trading_port, config)
    try:
        await client.connect()
        response_q = client.subscribe(MessageType.TRADE_ACCOUNT_RESPONSE)
        try:
            await client.send({
                "Type": int(MessageType.TRADE_ACCOUNTS_REQUEST),
                "RequestID": 1,
            })
            accounts: list[dict] = []
            try:
                first = await asyncio.wait_for(response_q.get(), timeout=10)
            except asyncio.TimeoutError:
                return {"ok": True, "count": 0, "accounts": []}
            accounts.append({"trade_account": first.get("TradeAccount")})
            total = int(first.get("TotalNumberMessages", 1) or 1)
            while len(accounts) < total:
                msg = await asyncio.wait_for(response_q.get(), timeout=10)
                accounts.append({"trade_account": msg.get("TradeAccount")})
            return {"ok": True, "count": len(accounts), "accounts": accounts}
        finally:
            client.unsubscribe(MessageType.TRADE_ACCOUNT_RESPONSE)
    finally:
        await client.close()


@mcp.tool()
async def get_account_balance(trade_account: str = "") -> dict:
    """Get current cash balance, margin and securities value for the given account.

    trade_account: Specific account string. Leave empty to get all accounts.
    """
    config = Config.from_env()
    client = DTCClient(config.host, config.trading_port, config)
    try:
        await client.connect()
        ok, result = await _collect_multi(
            client,
            {
                "Type": int(MessageType.ACCOUNT_BALANCE_REQUEST),
                "RequestID": 1,
                "TradeAccount": trade_account,
            },
            MessageType.ACCOUNT_BALANCE_UPDATE,
            MessageType.ACCOUNT_BALANCE_REJECT,
            no_items_key="NoAccountBalances",
            timeout=10,
        )
        if not ok:
            return {"ok": False, "reject_reason": result}

        balances = [
            {
                "trade_account": m.get("TradeAccount"),
                "currency": m.get("AccountCurrency"),
                "cash_balance": m.get("CashBalance"),
                "available_for_new_positions": m.get("BalanceAvailableForNewPositions"),
                "securities_value": m.get("SecuritiesValue"),
                "margin_requirement": m.get("MarginRequirement"),
                "open_position_profit_loss": m.get("OpenPositionsProfitLoss"),
                "daily_profit_loss": m.get("DailyProfitLoss"),
            }
            for m in result  # type: ignore[union-attr]
        ]
        return {"ok": True, "count": len(balances), "balances": balances}
    finally:
        await client.close()


@mcp.tool()
async def get_positions(trade_account: str = "") -> dict:
    """Get currently open positions for the given account.

    trade_account: Specific account string. Leave empty for all accounts.
    Quantity is signed: positive = long, negative = short.
    """
    config = Config.from_env()
    client = DTCClient(config.host, config.trading_port, config)
    try:
        await client.connect()
        ok, result = await _collect_multi(
            client,
            {
                "Type": int(MessageType.CURRENT_POSITIONS_REQUEST),
                "RequestID": 1,
                "TradeAccount": trade_account,
            },
            MessageType.POSITION_UPDATE,
            MessageType.CURRENT_POSITIONS_REJECT,
            no_items_key="NoPositions",
            timeout=10,
        )
        if not ok:
            return {"ok": False, "reject_reason": result}

        positions = [
            {
                "trade_account": m.get("TradeAccount"),
                "symbol": m.get("Symbol"),
                "exchange": m.get("Exchange"),
                "quantity": m.get("Quantity"),
                "average_price": m.get("AveragePrice"),
                "position_identifier": m.get("PositionIdentifier"),
            }
            for m in result  # type: ignore[union-attr]
        ]
        return {"ok": True, "count": len(positions), "positions": positions}
    finally:
        await client.close()


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
