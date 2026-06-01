# trading-brain

Personal trading copilot built as a set of MCP servers for Claude. Connects
Claude to **Sierra Chart** (live market data and account state) and **Discord**
(a trading-education community used as a knowledge source), with a local
SQLite journal for persistent memory.

## What's inside

| MCP | Purpose | Tools |
|---|---|---|
| **sierra-mcp** | Sierra Chart bridge (futures: ES, NQ, MES, MNQ on CME) | `ping_sierra`, `get_quote`, `get_recent_bars` (DTC), `get_recent_bars_scid` (local file), `get_latest_tick_scid`, `get_scid_status`, `get_futures_context`, `list_trade_accounts`, `get_account_balance`, `get_positions` |
| **discord-mcp** | Read-only Discord channels as a knowledge base | `list_servers`, `list_channels`, `read_recent_messages`, `search_messages`, `fetch_image`, `list_groups`, `read_group` |
| **journal-mcp** | Local SQLite memory for observations, trades, and reviews | Tools: `log_observation`, `log_trade`, `update_trade`, `search_journal`, `list_recent`, `daily_summary`, `list_workflows`, `premarket_review`, `postmarket_review`, `weekly_trading_review` |

Sierra and Discord are read-only. journal-mcp is the first write-capable MCP,
but it only writes to a local SQLite journal database. Order placement is still
out of scope until the read path and journal workflows are validated end-to-end.

## How they talk to Sierra Chart

Two paths, complementary:

1. **DTC Protocol** (`get_quote`, `get_recent_bars`, account / positions) — TCP
   JSON over Sierra's local DTC server. Requires Denali Exchange Data Feed +
   Service Package 11+ for full market-data redistribution. SC Data
   (free/delayed) refuses DTC redistribution.
2. **`.scid` file reader** (`get_recent_bars_scid`, `get_latest_tick_scid`) —
   reads Sierra's local tick-storage files directly from disk, aggregates ticks
   into bars on the fly, and can read the latest tick with file freshness.
   Works regardless of DTC permissions. ~Seconds of lag from real-time (file
   flush cadence).

`get_futures_context` is the main ES/NQ context pack: latest tick, freshness,
recent bars, approximate current Globex session OHLC/VWAP/delta, ATR, and a
simple bias read.

## Requirements

- Windows (Sierra Chart is Windows-native; paths assume `D:\SierraChart\Data\`)
- Python 3.11+
- [uv](https://docs.astral.sh/uv/) for env/lockfile management
- Sierra Chart account with DTC enabled (Global Settings → Sierra Chart Server
  Settings → Encoding: **JSON**)
- Discord bot token (Application from
  [discord.com/developers/applications](https://discord.com/developers/applications),
  Message Content Intent enabled)
- Claude Desktop

## Setup

```powershell
# Clone
git clone https://github.com/AndreuCrespo/trading-brain.git D:\inversion
cd D:\inversion

# Install each MCP (creates per-project .venv + uv.lock)
cd sierra-mcp  ; uv sync ; cd ..
cd discord-mcp ; uv sync ; cd ..
cd journal-mcp ; uv sync ; cd ..
```

Per project, copy `.env.example` → `.env` and fill in secrets. discord-mcp's
`.env`:
```
DISCORD_BOT_TOKEN=your-bot-token
```

sierra-mcp env vars (all optional, sensible defaults):
```
SIERRA_DTC_HOST=127.0.0.1
SIERRA_DTC_PORT=11099
SIERRA_DTC_HISTORICAL_PORT=11098
SIERRA_DATA_PATH=D:\SierraChart\Data
```

journal-mcp env vars (optional):
```
JOURNAL_DB_PATH=D:\inversion\journal-mcp\data\journal.db
```

## Wiring into Claude Desktop

Edit `%APPDATA%\Claude\claude_desktop_config.json` and add:

```json
{
  "mcpServers": {
    "sierra": {
      "command": "D:\\inversion\\sierra-mcp\\.venv\\Scripts\\python.exe",
      "args": ["D:\\inversion\\sierra-mcp\\src\\sierra_mcp\\server.py"]
    },
    "discord": {
      "command": "D:\\inversion\\discord-mcp\\.venv\\Scripts\\python.exe",
      "args": ["D:\\inversion\\discord-mcp\\src\\discord_mcp\\server.py"]
    },
    "journal": {
      "command": "D:\\inversion\\journal-mcp\\.venv\\Scripts\\python.exe",
      "args": ["D:\\inversion\\journal-mcp\\src\\journal_mcp\\server.py"]
    }
  }
}
```

Restart Claude Desktop fully (Quit from system tray, not minimise). The MCPs
appear under Configuración → Desarrollador.

> Note: args use the absolute file path instead of `python -m sierra_mcp.server`
> because Claude Desktop mangles the `ó` in the project path when invoking
> `-m` module mode. Loading the file directly works around this; each
> `server.py` has a `sys.path` shim at the top so its package imports resolve.

## Dev workflow (MCP Inspector)

```powershell
cd sierra-mcp          # or discord-mcp / journal-mcp
uv run mcp dev src/sierra_mcp/server.py
```

In the inspector UI, point Command at the per-project venv python and Args at
the file path (same reason as Claude Desktop):

- Command: `D:\inversion\sierra-mcp\.venv\Scripts\python.exe`
- Arguments: `D:\inversion\sierra-mcp\src\sierra_mcp\server.py`

## Discord channel taxonomy

discord-mcp groups channels into domain categories defined in
[`discord-mcp/groups.yml`](discord-mcp/groups.yml): `prep`, `post`, `recursos`,
`fondeo`, `futuros`, `stocks`. Matched by exact name or prefix. Edit the YAML
to add new channels — no code changes needed.

`read_group` fans out reads across every channel in a group in parallel:

```
read_group(server="...", group="post", limit_per_channel=5)
# → snapshot of postmarket-futuros, postmarket-stocks, setups-database,
#   writes-up all at once
```

## Journal memory

journal-mcp stores local memory in SQLite (default:
`journal-mcp/data/journal.db`, gitignored). It is intentionally simple:
observations and trades, searchable by text, symbol, tags, dates, and kind.

Use it for market observations, trade plans, post-mortems, recurring mistakes,
rules, and daily/weekly review raw material.

It also exposes workflow tools (and matching MCP prompts) that orchestrate the
other MCPs from Claude Desktop:

- `premarket_review`: reads Discord `prep`, Sierra futures context, and journal memory.
- `postmarket_review`: reads Discord `post`, chart screenshots, Sierra context, and journal memory.
- `weekly_trading_review`: summarizes the week from journal + Discord write-ups + current futures context.

Example flow:

```
log_observation(content="MES rejected VWAP after postmarket context", tags=["vwap", "post"], symbol="MES")
log_trade(symbol="MESM26-CME", side="long", entry_price=7590.5, quantity=1, account="Sim1", setup="EF")
daily_summary(date="2026-06-01")
```

## Example conversations

> *"Read the last 5 messages from the `post` group on Estudio trading donAdri,
> grab the last 10 1-min bars of MESM26-CME, and tell me whether anything
> discussed lines up with recent price action."*

> *"There's a 29-May post in postmarket-futuros with two attached screenshots.
> Read them and summarise what the EOM trade looked like."*

> *"Walk me through `📘15-theplan` and tell me how donAdri's system handles
> position sizing."*

> *"Log today's main mistake as a journal observation tagged `risk` and
> `discipline`, then show me the last 10 journal entries for MES."*

> Run the `premarket_review` prompt before the session, then save the final plan
> to the journal if it looks right.

## Status & known limitations

- ✅ `ping_sierra`, `get_recent_bars_scid` validated against live data
- ✅ `get_latest_tick_scid` smoke-tested on MESM26-CME with ~1-2s file/tick age
- ✅ `get_futures_context` smoke-tested on MESM26-CME using current Globex session
- ✅ Discord tools all validated against the live community server
- ✅ journal-mcp smoke-tested locally against SQLite
- ⚠️ `get_quote` / `get_recent_bars` (DTC) return *"Request is not authorized"*
  on the current account despite SP11 + Denali active. Workaround in place via
  `get_recent_bars_scid`; investigating with Sierra support.
- ⚠️ `get_account_balance` / `get_positions` return empty / timeout on the Sim
  accounts before any trade has been opened in Sierra. Validation pending.
- Read-only across the board. No order placement.

## Roadmap

Short term, in priority order:
1. Use journal-mcp in real Claude Desktop conversations and refine the schema
   from actual workflow pain.
2. Indicators/context on top of `.scid` bars: VWAP, ATR, RVOL, market profile,
   delta, `get_futures_context`.
3. If true bid/ask/DOM real-time is required, build an ACSIL bridge inside
   Sierra Chart that writes ticks/quotes/depth to a local file or socket.
4. Order placement (sim first, with explicit confirmation per call).

Longer term: more data sources as separate MCPs (SEC EDGAR, FRED, news), simple
backtest framework over `.scid`.

## Layout

```
inversion/
├── README.md
├── .gitignore
├── sierra-mcp/
│   ├── CLAUDE.md            # per-project guidance for future Claude sessions
│   ├── pyproject.toml
│   ├── uv.lock
│   └── src/sierra_mcp/
│       ├── server.py        # FastMCP tools
│       ├── dtc_client.py    # async DTC over TCP/JSON
│       ├── dtc_messages.py
│       ├── scid_reader.py   # local .scid tick file reader
│       └── config.py
└── discord-mcp/
    ├── pyproject.toml
    ├── uv.lock
    ├── groups.yml           # channel taxonomy (edit me, not code)
    └── src/discord_mcp/
        ├── server.py
        ├── client.py        # Discord REST API client
        ├── groups.py        # YAML loader + channel matcher
        └── config.py
└── journal-mcp/
    ├── pyproject.toml
    ├── uv.lock
    ├── data/                # local SQLite DBs, gitignored
    └── src/journal_mcp/
        ├── server.py        # FastMCP tools
        ├── db.py            # SQLite schema/helpers
        └── config.py
```
