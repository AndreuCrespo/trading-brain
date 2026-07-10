import asyncio
import logging
import os
import sys
import time
import uuid
from datetime import date, datetime, time as dt_time, timedelta, timezone
from pathlib import Path

# When loaded by `mcp dev` (which imports this file by path, not as a package),
# ensure the parent `src/` is on sys.path so the sierra_mcp.* imports resolve.
_src_dir = Path(__file__).resolve().parents[1]
if str(_src_dir) not in sys.path:
    sys.path.insert(0, str(_src_dir))

from mcp.server.fastmcp import FastMCP

from sierra_mcp.config import Config
from sierra_mcp.dtc_client import DTCClient
from sierra_mcp.dtc_messages import BuySell, MessageType, OpenCloseTrade, OrderType, TimeInForce
from sierra_mcp import indicator_engine, scid_reader

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

# Lookbacks are time-based (dense symbols like MNQ blow past any fixed record
# count within a couple of sessions); the record caps only bound memory.
SESSION_LOOKBACK_HOURS = 30
SESSION_LOOKBACK_MAX_RECORDS = 2_000_000
FEATURES_LOOKBACK_HOURS = 96
FEATURES_LOOKBACK_MAX_RECORDS = 6_000_000
FUTURES_ROOTS = {
    "ES": "E-mini S&P 500",
    "MES": "Micro E-mini S&P 500",
    "NQ": "E-mini Nasdaq 100",
    "MNQ": "Micro E-mini Nasdaq 100",
}
SIM_ALLOWED_ROOTS = {"MES", "MNQ"}
CME_QUARTERLY_MONTH_CODES = ((3, "H"), (6, "M"), (9, "U"), (12, "Z"))
CME_MONTH_CODE_TO_MONTH = {code: month for month, code in CME_QUARTERLY_MONTH_CODES}
DEFAULT_ROLL_DAYS_BEFORE_EXPIRY = 8
SIM_MAX_QUANTITY = 1
TERMINAL_ORDER_STATUSES = {7, 8, 9}
TERMINAL_ORDER_REASONS = {4, 6, 8, 9, 10}
ORDER_STATUS_NAMES = {
    0: "unspecified",
    1: "order_sent",
    2: "pending_open",
    3: "pending_child",
    4: "open",
    5: "pending_cancel_replace",
    6: "pending_cancel",
    7: "filled",
    8: "canceled",
    9: "rejected",
    10: "partially_filled",
}
ORDER_UPDATE_REASON_NAMES = {
    1: "open_orders_request_response",
    2: "new_order_accepted",
    3: "general_order_update",
    4: "order_filled",
    5: "order_filled_partially",
    6: "order_canceled",
    7: "order_cancel_replace_complete",
    8: "new_order_rejected",
    9: "order_cancel_rejected",
    10: "order_cancel_replace_rejected",
}


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


def _third_friday(year: int, month: int) -> date:
    first = date(year, month, 1)
    days_to_friday = (4 - first.weekday()) % 7
    first_friday = first + timedelta(days=days_to_friday)
    return first_friday + timedelta(days=14)


def _contract_symbol(root: str, year: int, month: int) -> str:
    month_code = dict(CME_QUARTERLY_MONTH_CODES)[month]
    return f"{root}{month_code}{str(year)[-2:]}"


def _active_contract_info(
    root: str,
    as_of: date | None = None,
    roll_days_before_expiry: int = DEFAULT_ROLL_DAYS_BEFORE_EXPIRY,
) -> dict:
    root = root.strip().upper()
    if root not in FUTURES_ROOTS:
        raise ValueError(f"root must be one of {sorted(FUTURES_ROOTS)}")
    roll_days_before_expiry = max(0, min(int(roll_days_before_expiry), 30))
    as_of = as_of or datetime.now(timezone.utc).date()

    for year in range(as_of.year, as_of.year + 3):
        for month, month_code in CME_QUARTERLY_MONTH_CODES:
            expiry = _third_friday(year, month)
            roll_date = expiry - timedelta(days=roll_days_before_expiry)
            if as_of <= roll_date:
                symbol = _contract_symbol(root, year, month)
                return {
                    "root": root,
                    "description": FUTURES_ROOTS[root],
                    "symbol": symbol,
                    "symbol_cme": f"{symbol}-CME",
                    "contract_month": month,
                    "contract_month_code": month_code,
                    "contract_year": year,
                    "expiration_date": expiry.isoformat(),
                    "roll_date": roll_date.isoformat(),
                    "roll_days_before_expiry": roll_days_before_expiry,
                    "as_of_date": as_of.isoformat(),
                }

    raise ValueError(f"could not resolve active contract for {root}")


def _parse_as_of_date(as_of_date: str) -> date | None:
    as_of_date = as_of_date.strip()
    if not as_of_date:
        return None
    return date.fromisoformat(as_of_date)


def _active_sim_symbols() -> set[str]:
    symbols: set[str] = set()
    for root in sorted(SIM_ALLOWED_ROOTS):
        info = _active_contract_info(root)
        symbols.add(info["symbol"])
        symbols.add(info["symbol_cme"])
    return symbols


def _sim_symbol_allowed(symbol: str) -> bool:
    symbol = symbol.strip().upper()
    return bool(_symbol_aliases(symbol) & _active_sim_symbols())


def _resolve_scid_path(data_path: str, symbol: str) -> tuple[str, str] | None:
    candidates: list[tuple[str, str, float, float]] = []
    seen: set[str] = set()
    for candidate in [symbol.strip().upper(), *_symbol_aliases(symbol)]:
        if candidate in seen:
            continue
        seen.add(candidate)
        path = os.path.join(data_path, f"{candidate}.scid")
        if os.path.exists(path):
            record = scid_reader.read_last_record(path)
            status = scid_reader.file_status(path)
            last_record_time = record.unix_time if record is not None else 0.0
            candidates.append((candidate, path, last_record_time, status["modified_unix"]))
    if not candidates:
        return None

    # Sierra may keep both MESM26.scid and MESM26-CME.scid. Pick the file with
    # the freshest tick first, then the freshest file modification time.
    best = max(candidates, key=lambda row: (row[2], row[3]))
    return best[0], best[1]


def _scid_freshness(config: Config, symbol: str) -> dict:
    """Report how fresh the local SCID data is for a symbol.

    Order tools use this as a market-context guard: no SCID file or a last tick
    older than order_max_tick_age_seconds means we have no current view of the
    market (Sierra closed, feed dead, market closed, or expired contract file).
    """
    max_age = config.order_max_tick_age_seconds
    resolved = _resolve_scid_path(config.data_path, symbol)
    if resolved is None:
        return {
            "ok": False,
            "stale": True,
            "error": f"no local .scid file found for {symbol} under {config.data_path}",
        }
    resolved_symbol, path = resolved
    record = scid_reader.read_last_record(path)
    if record is None:
        return {
            "ok": False,
            "stale": True,
            "resolved_symbol": resolved_symbol,
            "error": f"{resolved_symbol}.scid exists but has no tick records",
        }
    now = time.time()
    tick_age = max(0.0, now - record.unix_time)
    file_age = max(0.0, now - scid_reader.file_status(path)["modified_unix"])
    # tick_age includes the feed delay (10 min on Trading Evaluator - Delayed)
    # plus Sierra's batched disk writes, so a healthy delayed feed can show
    # 10-17 minutes. file_age tells us when Sierra last wrote anything, so the
    # freshest of the two is when we last learned something new about the market.
    age = min(tick_age, file_age)
    return {
        "ok": True,
        "resolved_symbol": resolved_symbol,
        "last_tick_time": datetime_from_unix(record.unix_time),
        "last_price": record.close,
        "tick_age_seconds": round(tick_age, 1),
        "file_age_seconds": round(file_age, 1),
        "freshness_age_seconds": round(age, 1),
        "max_tick_age_seconds": max_age,
        "stale": age > max_age,
        # A tick noticeably older than the file write usually means a delayed
        # feed; workflows should say so instead of presenting levels as live.
        "likely_delayed_feed": not (tick_age > max_age) and tick_age - file_age > 120,
    }


def _validate_sim_order(
    symbol: str,
    side: str,
    quantity: int,
    trade_account: str,
    allowed_sim_accounts: tuple[str, ...],
) -> dict | None:
    if not trade_account.startswith("Sim"):
        return {"ok": False, "error": "trade_account must start with 'Sim' for order tools"}
    if not allowed_sim_accounts:
        return {
            "ok": False,
            "error": "safety.json allowed_sim_accounts must be set before order tools can submit",
        }
    if trade_account not in allowed_sim_accounts:
        return {
            "ok": False,
            "error": f"trade_account not allowed for sim orders: {trade_account}",
            "allowed_accounts": list(allowed_sim_accounts),
        }
    allowed_symbols = _active_sim_symbols()
    if not _sim_symbol_allowed(symbol):
        return {
            "ok": False,
            "error": f"symbol not allowed for sim orders: {symbol}",
            "allowed_symbols": sorted(allowed_symbols),
            "allowed_roots": sorted(SIM_ALLOWED_ROOTS),
            "contract_policy": "current active quarterly MES/MNQ contracts only",
        }
    if side.lower() not in {"buy", "sell"}:
        return {"ok": False, "error": "side must be buy or sell"}
    if quantity < 1 or quantity > SIM_MAX_QUANTITY:
        return {"ok": False, "error": f"quantity must be between 1 and {SIM_MAX_QUANTITY}"}
    return None


def _format_order_update(m: dict) -> dict:
    order_status = m.get("OrderStatus")
    update_reason = m.get("OrderUpdateReason")
    return {
        "request_id": m.get("RequestID"),
        "message_number": m.get("MessageNumber"),
        "total_num_messages": m.get("TotalNumMessages") or m.get("TotalNumberMessages"),
        "no_orders": bool(m.get("NoOrders")),
        "symbol": m.get("Symbol"),
        "exchange": m.get("Exchange"),
        "trade_account": m.get("TradeAccount"),
        "client_order_id": m.get("ClientOrderID"),
        "server_order_id": m.get("ServerOrderID"),
        "order_status": order_status,
        "order_status_name": ORDER_STATUS_NAMES.get(order_status),
        "order_update_reason": update_reason,
        "order_update_reason_name": ORDER_UPDATE_REASON_NAMES.get(update_reason),
        "order_type": m.get("OrderType"),
        "buy_sell": m.get("BuySell"),
        "price1": m.get("Price1"),
        "price2": m.get("Price2"),
        "quantity": m.get("OrderQuantity", m.get("Quantity")),
        "filled_quantity": m.get("FilledQuantity"),
        "remaining_quantity": m.get("RemainingQuantity"),
        "average_fill_price": m.get("AverageFillPrice"),
        "last_fill_price": m.get("LastFillPrice"),
        "info_text": m.get("InfoText"),
        "free_form_text": m.get("FreeFormText"),
    }


def _format_position(m: dict) -> dict:
    return {
        "trade_account": m.get("TradeAccount"),
        "symbol": m.get("Symbol"),
        "exchange": m.get("Exchange"),
        "quantity": m.get("Quantity"),
        "average_price": m.get("AveragePrice"),
        "position_identifier": m.get("PositionIdentifier"),
    }


def _symbol_aliases(symbol: str) -> set[str]:
    symbol = symbol.strip().upper()
    aliases = {symbol}
    if symbol.endswith("-CME"):
        aliases.add(symbol.removesuffix("-CME"))
    else:
        aliases.add(f"{symbol}-CME")
    return aliases


def _symbols_match(left: str | None, right: str | None) -> bool:
    if not left or not right:
        return False
    return bool(_symbol_aliases(left) & _symbol_aliases(right))


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
async def get_active_futures_contract(
    root: str = "MES",
    as_of_date: str = "",
    roll_days_before_expiry: int = DEFAULT_ROLL_DAYS_BEFORE_EXPIRY,
) -> dict:
    """Resolve the active quarterly CME equity-index futures contract.

    root: one of ES, MES, NQ, MNQ.
    as_of_date: optional YYYY-MM-DD override for deterministic rollover checks.
    roll_days_before_expiry: default 8, so the tool rolls before expiration week.

    Use this before market reads or sim orders when the user says "MES", "MNQ",
    "ES" or "Nasdaq" without an explicit contract month.
    """
    try:
        as_of = _parse_as_of_date(as_of_date)
        info = _active_contract_info(root, as_of, roll_days_before_expiry)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, **info}


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

    symbol: Sierra Chart contract symbol. Resolve current front-month contracts
            with get_active_futures_contract first, e.g. root="MES" -> "MESU26"
            after June 2026 rollover.
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

    symbol: Contract symbol, e.g. "MESU26", "MNQU26".
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

    symbol: e.g. "MESU26". A matching .scid alias must exist in the data dir.
    interval: One of "1m", "5m", "15m", "30m", "1h", "4h", "1d".
    count: Number of most recent bars to return (default 100, cap 5000).
    """
    if interval not in INTERVAL_SECONDS or interval in ("tick", "1s"):
        return {"ok": False, "error": "interval must be 1m/5m/15m/30m/1h/4h/1d"}

    interval_sec = INTERVAL_SECONDS[interval]
    count = max(1, min(count, 5000))

    config = Config.from_env()
    resolved = _resolve_scid_path(config.data_path, symbol)
    if resolved is None:
        return {
            "ok": False,
            "error": f"file not found for {symbol}",
            "tried_symbols": sorted(_symbol_aliases(symbol)),
            "data_path": config.data_path,
        }
    resolved_symbol, scid_path = resolved

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
        "resolved_symbol": resolved_symbol,
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
    resolved = _resolve_scid_path(config.data_path, symbol)
    if resolved is None:
        return {
            "ok": False,
            "error": f"file not found for {symbol}",
            "tried_symbols": sorted(_symbol_aliases(symbol)),
            "data_path": config.data_path,
        }
    resolved_symbol, scid_path = resolved

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
        "resolved_symbol": resolved_symbol,
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
    resolved = _resolve_scid_path(config.data_path, symbol)
    if resolved is None:
        return {
            "ok": False,
            "error": f"file not found for {symbol}",
            "tried_symbols": sorted(_symbol_aliases(symbol)),
            "data_path": config.data_path,
        }
    resolved_symbol, scid_path = resolved

    record = await asyncio.to_thread(scid_reader.read_last_record, scid_path)
    status = await asyncio.to_thread(scid_reader.file_status, scid_path)
    now = time.time()
    out = {
        "ok": True,
        "symbol": symbol,
        "resolved_symbol": resolved_symbol,
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
    resolved = _resolve_scid_path(config.data_path, symbol)
    if resolved is None:
        return {
            "ok": False,
            "error": f"file not found for {symbol}",
            "tried_symbols": sorted(_symbol_aliases(symbol)),
            "data_path": config.data_path,
        }
    resolved_symbol, scid_path = resolved

    records = await asyncio.to_thread(
        scid_reader.read_records_since,
        scid_path,
        time.time() - SESSION_LOOKBACK_HOURS * 3600,
        SESSION_LOOKBACK_MAX_RECORDS,
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
        "resolved_symbol": resolved_symbol,
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


@mcp.tool()
async def get_market_features(
    symbol: str,
    interval: str = "1m",
    bars_count: int = 30,
    tick_size: float = 0.25,
    value_area_percent: float = 0.70,
) -> dict:
    """Calculate structured market features from Sierra's local .scid file.

    This is the indicator-engine path for the trading system: it calculates
    VWAP, value area, POC, delta, range and price location from raw tick records
    instead of reading visual studies from a chart. It returns current and
    previous approximate CME equity-index Globex sessions.
    """
    if interval not in INTERVAL_SECONDS or interval in ("tick", "1s"):
        return {"ok": False, "error": "interval must be 1m/5m/15m/30m/1h/4h/1d"}
    if tick_size <= 0:
        return {"ok": False, "error": "tick_size must be positive"}
    if not 0.50 <= value_area_percent <= 0.90:
        return {"ok": False, "error": "value_area_percent must be between 0.50 and 0.90"}

    bars_count = max(5, min(bars_count, 500))
    interval_sec = INTERVAL_SECONDS[interval]

    config = Config.from_env()
    resolved = _resolve_scid_path(config.data_path, symbol)
    if resolved is None:
        aliases = sorted(_symbol_aliases(symbol))
        return {
            "ok": False,
            "error": f"file not found for {symbol}",
            "tried_symbols": aliases,
            "data_path": config.data_path,
        }
    resolved_symbol, scid_path = resolved

    records = await asyncio.to_thread(
        scid_reader.read_records_since,
        scid_path,
        time.time() - FEATURES_LOOKBACK_HOURS * 3600,
        FEATURES_LOOKBACK_MAX_RECORDS,
    )
    if not records:
        return {"ok": False, "error": f"no records in file: {scid_path}"}

    last_record = records[-1]
    features = indicator_engine.build_market_features(records, tick_size, value_area_percent)
    bars = scid_reader.aggregate_to_bars(records, interval_sec)[-bars_count:]
    status = await asyncio.to_thread(scid_reader.file_status, scid_path)

    latest_price = float(last_record.close)
    current_start = features["current_start"]
    previous_start = features["previous_start"]
    current_records = features["current_records"]
    previous_records = features["previous_records"]
    current = features["current_session"]
    previous = features["previous_session"]

    warnings: list[str] = []
    now = time.time()
    tick_age = max(0.0, now - last_record.unix_time)
    file_age = max(0.0, now - status["modified_unix"])
    if file_age > 10:
        warnings.append(f"SCID file is stale by {file_age:.1f}s")
    if tick_age > 30:
        warnings.append(f"last tick is stale by {tick_age:.1f}s")
    if not previous:
        warnings.append("previous Globex session unavailable in loaded SCID window")
    coverage_start = records[0].unix_time
    week_anchor = indicator_engine._week_start(last_record.unix_time)
    month_anchor = indicator_engine._month_start(last_record.unix_time)
    if coverage_start > min(week_anchor, month_anchor) + 60:
        warnings.append(
            "anchored weekly/monthly VWAPs are partial: loaded window starts at "
            f"{datetime_from_unix(coverage_start)}"
        )

    return {
        "ok": True,
        "symbol": symbol,
        "resolved_symbol": resolved_symbol,
        "source": scid_path,
        "latest": {
            "price": latest_price,
            "time": datetime_from_unix(last_record.unix_time),
            "tick_age_seconds": round(tick_age, 3),
            "file_age_seconds": round(file_age, 3),
            "volume": last_record.volume,
            "bid_volume": last_record.bid_volume,
            "ask_volume": last_record.ask_volume,
        },
        "settings": {
            "interval": interval,
            "bars_count": bars_count,
            "tick_size": tick_size,
            "value_area_percent": value_area_percent,
            "session_model": "approx_cme_equity_globex_22utc",
            "current_session_start": datetime_from_unix(current_start),
            "previous_session_start": datetime_from_unix(previous_start) if previous_start is not None else None,
        },
        "current_session": current,
        "previous_session": previous,
        "relationships": features["relationships"],
        "derived_levels": features["derived_levels"],
        "read": {
            "tags": features["read_tags"],
            "warnings": warnings,
        },
        "bars": bars,
        "ticks_scanned": len(records),
        "session_ticks": {
            "current": len(current_records),
            "previous": len(previous_records),
        },
        "file": {
            "record_count": status["record_count"],
            "modified_time": status["modified_time"],
        },
    }


@mcp.tool()
async def get_indicator_levels(
    symbol: str,
    tick_size: float = 0.25,
    value_area_percent: float = 0.70,
) -> dict:
    """Return a compact indicator-level pack calculated from local SCID ticks.

    Use this when the user asks for "indicadores", VWAP/value levels, IB/ON,
    ADR or anchored VWAPs. This is a compact view over `get_market_features`:
    it does not read Sierra Chart visual studies and it inherits SCID/feed delay.
    """
    features = await get_market_features(
        symbol=symbol,
        interval="1m",
        bars_count=5,
        tick_size=tick_size,
        value_area_percent=value_area_percent,
    )
    if not features.get("ok"):
        return features

    current = features.get("current_session", {})
    previous = features.get("previous_session", {})
    current_profile = current.get("volume_profile", {})
    previous_profile = previous.get("volume_profile", {})
    derived = features.get("derived_levels", {})

    return {
        "ok": True,
        "symbol": features.get("symbol"),
        "resolved_symbol": features.get("resolved_symbol"),
        "source": features.get("source"),
        "latest": features.get("latest"),
        "settings": features.get("settings"),
        "current": {
            "vwap": current.get("vwap"),
            "poc": current_profile.get("poc"),
            "vah": current_profile.get("vah"),
            "val": current_profile.get("val"),
            "high": current.get("high"),
            "low": current.get("low"),
            "volume": current.get("volume"),
            "delta": current.get("delta"),
        },
        "previous": {
            "vwap": previous.get("vwap"),
            "poc": previous_profile.get("poc"),
            "vah": previous_profile.get("vah"),
            "val": previous_profile.get("val"),
            "high": previous.get("high"),
            "low": previous.get("low"),
            "volume": previous.get("volume"),
            "delta": previous.get("delta"),
        },
        "playbook_levels": {
            "overnight": derived.get("overnight"),
            "initial_balance": derived.get("initial_balance"),
            "previous_day": derived.get("previous_day"),
            "adr": derived.get("adr"),
            "anchored_vwaps": derived.get("anchored_vwaps"),
            "distances": derived.get("distances"),
        },
        "relationships": features.get("relationships"),
        "read": features.get("read"),
    }


def _normalize_level_name(name: str) -> str:
    return "".join(ch for ch in name.lower() if ch.isalnum())


def _indicator_level_map(indicators: dict) -> dict[str, float]:
    current = indicators.get("current", {})
    previous = indicators.get("previous", {})
    playbook = indicators.get("playbook_levels", {})
    overnight = playbook.get("overnight") or {}
    initial_balance = playbook.get("initial_balance") or {}
    previous_day = playbook.get("previous_day") or {}
    anchored = playbook.get("anchored_vwaps") or {}
    weekly = anchored.get("weekly") or {}
    monthly = anchored.get("monthly") or {}

    raw_levels = {
        "vwap": current.get("vwap"),
        "current_vwap": current.get("vwap"),
        "poc": current.get("poc"),
        "current_poc": current.get("poc"),
        "vah": current.get("vah"),
        "current_vah": current.get("vah"),
        "val": current.get("val"),
        "current_val": current.get("val"),
        "previous_vwap": previous.get("vwap"),
        "pvwap": previous.get("vwap"),
        "previous_poc": previous.get("poc"),
        "ppoc": previous.get("poc"),
        "previous_vah": previous.get("vah"),
        "pvah": previous.get("vah"),
        "previous_val": previous.get("val"),
        "pval": previous.get("val"),
        "onh": overnight.get("high"),
        "onl": overnight.get("low"),
        "ibh": initial_balance.get("high"),
        "ibl": initial_balance.get("low"),
        "phod": previous_day.get("high"),
        "plod": previous_day.get("low"),
        "weekly_vwap": weekly.get("vwap"),
        "wvwap": weekly.get("vwap"),
        "monthly_vwap": monthly.get("vwap"),
        "mvwap": monthly.get("vwap"),
    }
    return {
        _normalize_level_name(name): float(value)
        for name, value in raw_levels.items()
        if value is not None
    }


@mcp.tool()
async def compare_indicator_levels(
    symbol: str,
    reference_levels: dict,
    tick_size: float = 0.25,
    tolerance_ticks: float = 2.0,
    value_area_percent: float = 0.70,
) -> dict:
    """Compare MCP-calculated indicator levels against Sierra visual-study values.

    `reference_levels` should be a mapping of level names to prices, for example:
    {"vwap": 7436.25, "poc": 7445.0, "vah": 7470.5, "val": 7424.0}.

    Supported names include vwap/current_vwap, poc, vah, val, pVAH, pVAL, pPOC,
    pVWAP, ONH, ONL, IBH, IBL, pHOD, pLOD, wVWAP and mVWAP. The comparison is
    useful for calibrating session templates, tick size and value-area settings.
    """
    if tick_size <= 0:
        return {"ok": False, "error": "tick_size must be positive"}
    if tolerance_ticks < 0:
        return {"ok": False, "error": "tolerance_ticks must be non-negative"}
    if not isinstance(reference_levels, dict) or not reference_levels:
        return {"ok": False, "error": "reference_levels must be a non-empty object"}

    indicators = await get_indicator_levels(
        symbol=symbol,
        tick_size=tick_size,
        value_area_percent=value_area_percent,
    )
    if not indicators.get("ok"):
        return indicators

    calculated = _indicator_level_map(indicators)
    comparisons = []
    unknown_levels = []
    for name, ref_value in reference_levels.items():
        normalized = _normalize_level_name(str(name))
        calc_value = calculated.get(normalized)
        if calc_value is None:
            unknown_levels.append(str(name))
            comparisons.append({
                "name": str(name),
                "ok": False,
                "error": "unknown or unavailable calculated level",
            })
            continue

        try:
            reference = float(ref_value)
        except (TypeError, ValueError):
            comparisons.append({
                "name": str(name),
                "ok": False,
                "error": "reference value must be numeric",
            })
            continue

        diff_points = round(calc_value - reference, 6)
        diff_ticks = round(diff_points / tick_size, 3)
        within = abs(diff_ticks) <= tolerance_ticks
        comparisons.append({
            "name": str(name),
            "ok": within,
            "calculated": calc_value,
            "reference": reference,
            "diff_points": diff_points,
            "diff_ticks": diff_ticks,
        })

    comparable = [row for row in comparisons if "diff_ticks" in row]
    return {
        "ok": True,
        "symbol": indicators.get("symbol"),
        "resolved_symbol": indicators.get("resolved_symbol"),
        "tolerance_ticks": tolerance_ticks,
        "all_within_tolerance": bool(comparable) and all(row["ok"] for row in comparable),
        "comparisons": comparisons,
        "unknown_levels": unknown_levels,
        "available_level_names": sorted(calculated),
        "latest": indicators.get("latest"),
        "read": indicators.get("read"),
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


async def _collect_positions(config: Config, trade_account: str = "") -> tuple[bool, list[dict] | str]:
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
            return False, result
        return True, [_format_position(m) for m in result]  # type: ignore[union-attr]
    finally:
        await client.close()


async def _submit_sim_market_order(
    config: Config,
    symbol: str,
    side_norm: str,
    quantity: int,
    trade_account: str,
    rationale: str,
    open_or_close: OpenCloseTrade,
    preview: dict,
) -> dict:
    client_order_id = f"tb-{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}"
    buy_sell = BuySell.BUY if side_norm == "buy" else BuySell.SELL

    client = DTCClient(config.host, config.trading_port, config)
    try:
        await client.connect()
        response_q = client.subscribe(MessageType.ORDER_UPDATE)
        try:
            await client.send({
                "Type": int(MessageType.SUBMIT_NEW_SINGLE_ORDER),
                "Symbol": symbol,
                "Exchange": "CME",
                "ClientOrderID": client_order_id,
                "OrderType": int(OrderType.MARKET),
                "BuySell": int(buy_sell),
                "Price1": 0,
                "Price2": 0,
                "TimeInForce": int(TimeInForce.DAY),
                "GoodTillDateTime": 0,
                "Quantity": quantity,
                "TradeAccount": trade_account,
                "IsAutomatedOrder": 1,
                "IsParentOrder": 0,
                "FreeFormText": f"trading-brain sim: {rationale}"[:120],
                "OpenOrClose": int(open_or_close),
                "MaxShowQuantity": 0,
                "Price1AsString": "",
                "Price2AsString": "",
                "IntendedPositionQuantity": 0,
            })

            updates: list[dict] = []
            deadline = time.time() + 20
            terminal = False
            while time.time() < deadline:
                try:
                    msg = await asyncio.wait_for(response_q.get(), timeout=deadline - time.time())
                except asyncio.TimeoutError:
                    break
                if msg.get("ClientOrderID") == client_order_id:
                    updates.append(_format_order_update(msg))
                    reason = msg.get("OrderUpdateReason")
                    status = msg.get("OrderStatus")
                    if status in TERMINAL_ORDER_STATUSES or reason in TERMINAL_ORDER_REASONS:
                        terminal = True
                        break

            if not updates:
                return {
                    "ok": False,
                    "error": "order submitted but no matching ORDER_UPDATE received",
                    "client_order_id": client_order_id,
                    "preview": preview,
                }

            last = updates[-1]
            rejected = last.get("order_update_reason") == 8 or last.get("order_status") == 9
            return {
                "ok": not rejected,
                "client_order_id": client_order_id,
                "preview": preview,
                "updates": updates,
                "last_update": last,
                "terminal": terminal,
                "message": (
                    "terminal order update received"
                    if terminal
                    else "order accepted/updated but no terminal fill/cancel/reject received before timeout"
                ),
            }
        finally:
            client.unsubscribe(MessageType.ORDER_UPDATE)
    finally:
        await client.close()


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
    ok, result = await _collect_positions(config, trade_account)
    if not ok:
        return {"ok": False, "reject_reason": result}
    positions = result  # type: ignore[assignment]
    return {"ok": True, "count": len(positions), "positions": positions}


@mcp.tool()
async def get_open_orders(trade_account: str = "") -> dict:
    """Get currently open/working orders for the given account.

    trade_account: Specific account string. Leave empty for all accounts.
    """
    config = Config.from_env()
    client = DTCClient(config.host, config.trading_port, config)
    try:
        await client.connect()
        ok, result = await _collect_multi(
            client,
            {
                "Type": int(MessageType.OPEN_ORDERS_REQUEST),
                "RequestID": 1,
                "RequestAllOrders": 1,
                "ServerOrderID": "",
                "TradeAccount": trade_account,
            },
            MessageType.ORDER_UPDATE,
            MessageType.OPEN_ORDERS_REQUEST_REJECT,
            no_items_key="NoOrders",
            timeout=10,
        )
        if not ok:
            return {"ok": False, "reject_reason": result}

        orders = [_format_order_update(m) for m in result]  # type: ignore[arg-type]
        orders = [o for o in orders if not o.get("no_orders")]
        return {"ok": True, "count": len(orders), "orders": orders}
    finally:
        await client.close()


@mcp.tool()
async def place_sim_market_order(
    symbol: str,
    side: str,
    quantity: int = 1,
    trade_account: str = "Sim1",
    rationale: str = "",
    confirm: bool = False,
) -> dict:
    """Place a tightly-guarded market order in Sierra Chart Trade Simulation Mode.

    Safety rules:
    - trade_account must start with Sim
    - symbol must be an active quarterly MES/MNQ contract
    - quantity is capped at 1
    - confirm must be true
    - rationale is required
    - local SCID data for the symbol must be fresh (order_max_tick_age_seconds);
      stale or missing data blocks new positions

    If confirm is false, returns an order preview and does not send anything.
    """
    symbol = symbol.strip().upper()
    side_norm = side.strip().lower()
    trade_account = trade_account.strip()
    rationale = rationale.strip()
    config = Config.from_env()

    validation_error = _validate_sim_order(
        symbol,
        side_norm,
        quantity,
        trade_account,
        config.allowed_sim_accounts,
    )
    if validation_error:
        return validation_error
    if not rationale:
        return {"ok": False, "error": "rationale is required"}

    market_data = _scid_freshness(config, symbol)
    if market_data.get("stale"):
        return {
            "ok": False,
            "error": (
                "market data guard: local SCID data for this symbol is stale or "
                "missing; refusing to open a new sim position without a current "
                "view of the market"
            ),
            "market_data": market_data,
            "hint": (
                "check get_scid_status; if Sierra Chart is running and the feed "
                "is just slower, raise SIERRA_ORDER_MAX_TICK_AGE_SECONDS in .env"
            ),
        }

    preview = {
        "symbol": symbol,
        "side": side_norm,
        "quantity": quantity,
        "trade_account": trade_account,
        "order_type": "market",
        "rationale": rationale,
        "market_data": market_data,
        "safety": {
            "sim_account_only": True,
            "allowed_accounts": list(config.allowed_sim_accounts),
            "allowed_symbols": sorted(_active_sim_symbols()),
            "max_quantity": SIM_MAX_QUANTITY,
            "max_tick_age_seconds": config.order_max_tick_age_seconds,
        },
    }
    if not confirm:
        return {
            "ok": False,
            "requires_confirmation": True,
            "message": "Set confirm=true to submit this SIM market order.",
            "preview": preview,
        }

    return await _submit_sim_market_order(
        config,
        symbol,
        side_norm,
        quantity,
        trade_account,
        rationale,
        OpenCloseTrade.UNSPECIFIED,
        preview,
    )


@mcp.tool()
async def close_sim_position(
    symbol: str,
    trade_account: str = "SimTB1",
    rationale: str = "",
    confirm: bool = False,
) -> dict:
    """Close the current simulated/evaluator position for a symbol with guardrails.

    Reads the current position first, calculates the opposite market order, and
    returns a preview unless confirm is true. This is the preferred tool for
    natural-language requests like "close MES".
    """
    symbol = symbol.strip().upper()
    trade_account = trade_account.strip()
    rationale = rationale.strip()
    config = Config.from_env()

    validation_error = _validate_sim_order(
        symbol,
        "buy",
        1,
        trade_account,
        config.allowed_sim_accounts,
    )
    if validation_error:
        return validation_error
    if not rationale:
        return {"ok": False, "error": "rationale is required"}

    ok, result = await _collect_positions(config, trade_account)
    if not ok:
        return {"ok": False, "reject_reason": result}

    positions = result  # type: ignore[assignment]
    matches = [
        p for p in positions
        if _symbols_match(p.get("symbol"), symbol) and float(p.get("quantity") or 0) != 0
    ]
    if not matches:
        return {
            "ok": True,
            "flat": True,
            "message": f"no open position found for {symbol} in {trade_account}",
            "positions": positions,
        }
    if len(matches) > 1:
        return {
            "ok": False,
            "error": f"multiple matching positions found for {symbol}; close manually",
            "matches": matches,
        }

    position = matches[0]
    position_qty = float(position.get("quantity") or 0)
    close_qty = int(abs(position_qty))
    if close_qty < 1:
        return {"ok": True, "flat": True, "message": f"position quantity is flat for {symbol}"}
    if close_qty > SIM_MAX_QUANTITY:
        return {
            "ok": False,
            "error": f"position quantity {close_qty} exceeds max close quantity {SIM_MAX_QUANTITY}",
            "position": position,
        }

    close_side = "sell" if position_qty > 0 else "buy"
    order_symbol = str(position.get("symbol") or symbol).strip().upper()
    # Closing reduces risk, so stale data warns but never blocks the close.
    market_data = _scid_freshness(config, order_symbol)
    preview = {
        "action": "close_sim_position",
        "symbol": order_symbol,
        "requested_symbol": symbol,
        "side": close_side,
        "quantity": close_qty,
        "trade_account": trade_account,
        "order_type": "market",
        "rationale": rationale,
        "position": position,
        "market_data": market_data,
        "safety": {
            "sim_account_only": True,
            "allowed_accounts": list(config.allowed_sim_accounts),
            "allowed_symbols": sorted(_active_sim_symbols()),
            "max_quantity": SIM_MAX_QUANTITY,
        },
    }
    if market_data.get("stale"):
        preview["warning"] = (
            "local SCID data is stale or missing; the close is still allowed "
            "but the fill price may differ from the last known price"
        )
    if not confirm:
        return {
            "ok": False,
            "requires_confirmation": True,
            "message": "Set confirm=true to submit this SIM close order.",
            "preview": preview,
        }

    order_result = await _submit_sim_market_order(
        config,
        order_symbol,
        close_side,
        close_qty,
        trade_account,
        rationale,
        OpenCloseTrade.CLOSE,
        preview,
    )
    await asyncio.sleep(0.5)
    post_ok, post_result = await _collect_positions(config, trade_account)
    order_result["post_check"] = (
        {
            "ok": True,
            "positions": [
                p for p in post_result  # type: ignore[union-attr]
                if _symbols_match(p.get("symbol"), symbol)
            ],
        }
        if post_ok
        else {"ok": False, "reject_reason": post_result}
    )
    return order_result


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
