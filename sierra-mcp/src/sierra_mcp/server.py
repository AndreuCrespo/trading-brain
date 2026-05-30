import asyncio
import logging
import os
import time

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
                "Type": int(MessageType.POSITIONS_REQUEST),
                "RequestID": 1,
                "TradeAccount": trade_account,
            },
            MessageType.POSITION_UPDATE,
            MessageType.POSITION_REQUEST_REJECT,
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
