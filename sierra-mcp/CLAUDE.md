# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

An MCP (Model Context Protocol) server that bridges Claude to **Sierra Chart** via the **DTC Protocol**. Targets ES and NQ futures trading. Current scope is **read-only** — quotes, positions, balance, historical bars. Order placement is deliberately out of scope until the read path is validated.

## Commands

Project uses **uv** for env + lockfile management. The `.venv` is per-project.

```powershell
# one-time setup (from project root) — installs deps from uv.lock into .venv
uv sync

# run the MCP server directly (stdio transport — for debugging)
uv run python -m sierra_mcp.server

# inspect with the MCP CLI inspector
uv run mcp dev src/sierra_mcp/server.py
```

When configuring the MCP Inspector UI (because mcp dev's default uv-based spawn
mangles non-ASCII paths on Windows), point Command at the per-project venv
python directly:
- Command: `D:\inversion\sierra-mcp\.venv\Scripts\python.exe`
- Args: `-m sierra_mcp.server`

No test suite or linter is configured yet.

## Sierra Chart configuration required

The client assumes Sierra Chart is already speaking JSON — there is no encoding negotiation handshake. In *Global Settings → Sierra Chart Server Settings*:

- `Enable DTC Protocol Server` = Yes
- `Encoding` = **JSON** (critical)
- `Listening Port` = 11099 (trading/market data)
- `Historical Data Port` = 11098

For local chart-only simulation, enable *Trade -> Trade Simulation Mode*. For server-side demo trading with persistent account state, use Sierra's Trading Evaluator / Trading Evaluator - Delayed service and keep local Trade Simulation Mode off.

Connection params come from env vars (see `config.py`): `SIERRA_DTC_HOST`, `SIERRA_DTC_PORT`, `SIERRA_DTC_HISTORICAL_PORT`, `SIERRA_DTC_USERNAME`, `SIERRA_DTC_PASSWORD`, `SIERRA_DTC_CLIENT_NAME`, `SIERRA_DTC_HEARTBEAT`, `SIERRA_ALLOWED_SIM_ACCOUNTS`.

## Architecture

Three layers, top to bottom:

1. **`server.py`** — `FastMCP` server. Each `@mcp.tool()` is what Claude sees. Tools currently open a fresh `DTCClient`, do their work, and close it in `finally`. This is intentional for the read-only phase; if we add long-lived subscriptions (streaming quotes, position updates), the client should move to module-level lifespan management.

2. **`dtc_client.py`** — async DTC client over `asyncio.open_connection`. The wire format is JSON objects terminated by `\x00`. The key pattern is **subscribe-then-send**:
   ```python
   queue = client._subscribe(MessageType.SOME_RESPONSE)
   try:
       await client._send({"Type": MessageType.SOME_REQUEST, ...})
       response = await queue.get()
   finally:
       client._unsubscribe(MessageType.SOME_RESPONSE)
   ```
   Always subscribe *before* sending — the response may arrive before the await otherwise. A background `_read_loop` dispatches incoming messages to subscriber queues by `Type`. Heartbeats are sent on `_heartbeat_loop` at `config.heartbeat_interval`.

3. **`dtc_messages.py`** — `MessageType` IntEnum for DTC message codes. Most field names in messages follow CamelCase per the DTC spec (`Username`, `HeartbeatIntervalInSeconds`, `Result`, etc.).

### Two separate connections

Sierra Chart exposes **two distinct TCP servers**: trading/market data (`trading_port`, 11099) and historical data (`historical_port`, 11098). They require separate `DTCClient` instances. Don't try to multiplex historical requests over the trading port.

### Multi-message responses

Some DTC requests yield a stream of responses (positions, historical bars), terminated by a final message with a `IsFinalMessage`/`MessageNumber == TotalNumberMessages` flag. The current `_subscribe`/queue pattern handles single-response requests; multi-message requests need to loop on the queue until the terminator arrives. Build this into the tool, not into `DTCClient`, to keep the client generic.

## Adding a new tool

1. Find the request/response message types in `dtc_messages.py` (add if missing).
2. In `server.py`, add an `@mcp.tool()` async function. Open a `DTCClient` on the right port, `connect()`, subscribe to the response type, send the request, await the response, close.
3. Return a plain dict — `FastMCP` serializes it for Claude.

## Known unknowns

The exact DTC field names (e.g., `TradingIsSupported` vs `TradeIsSupported`) and a few of the less-common message type numbers were written from spec memory and need verification against Sierra Chart's actual responses. If a tool returns empty/wrong data, check Sierra Chart's *Message Log* window first — it shows the raw JSON in both directions.
