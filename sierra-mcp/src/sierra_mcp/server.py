import logging

from mcp.server.fastmcp import FastMCP

from sierra_mcp.config import Config
from sierra_mcp.dtc_client import DTCClient
from sierra_mcp.dtc_messages import MessageType

logging.basicConfig(level=logging.INFO)

mcp = FastMCP("sierra-mcp")


MARKET_DATA_REQUEST_SNAPSHOT = 3


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
        response = await client.request(
            {
                "Type": int(MessageType.MARKET_DATA_REQUEST),
                "RequestAction": MARKET_DATA_REQUEST_SNAPSHOT,
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


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
