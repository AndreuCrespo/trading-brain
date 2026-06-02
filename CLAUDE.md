# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project North Star

This repository is a local trading copilot toolkit built around MCP servers. The long-term direction is a personal "Trading Brain": Claude/Codex reasons over Sierra Chart market data, the Estudio trading donAdri Discord knowledge base, and a future persistent journal/backtest layer.

Today the repo contains three MCP servers:

- `sierra-mcp`: Sierra Chart bridge for futures data and account state.
- `discord-mcp`: Discord read-only knowledge source with channel taxonomy.
- `journal-mcp`: SQLite-backed local memory for observations and trades.

Keep the system modular. New data sources should generally be separate MCPs rather than expanding one large server.

## Current Commands

The repo uses `uv` with one virtual environment per MCP project.

```powershell
# Install/update Sierra MCP deps
cd D:\inversion\sierra-mcp
uv sync

# Install/update Discord MCP deps
cd D:\inversion\discord-mcp
uv sync

# Install/update Journal MCP deps
cd D:\inversion\journal-mcp
uv sync
```

Run the MCP Inspector during development:

```powershell
cd D:\inversion\sierra-mcp
uv run mcp dev src/sierra_mcp/server.py

cd D:\inversion\discord-mcp
uv run mcp dev src/discord_mcp/server.py

cd D:\inversion\journal-mcp
uv run mcp dev src/journal_mcp/server.py
```

In the Inspector UI, prefer direct Python + file path because the old accented repo path caused module-path issues on Windows:

```text
Sierra command:
D:\inversion\sierra-mcp\.venv\Scripts\python.exe

Sierra args:
D:\inversion\sierra-mcp\src\sierra_mcp\server.py

Discord command:
D:\inversion\discord-mcp\.venv\Scripts\python.exe

Discord args:
D:\inversion\discord-mcp\src\discord_mcp\server.py

Journal command:
D:\inversion\journal-mcp\.venv\Scripts\python.exe

Journal args:
D:\inversion\journal-mcp\src\journal_mcp\server.py
```

Claude Desktop is wired through `%APPDATA%\Claude\claude_desktop_config.json` using the same direct file-path style. After editing that config, fully quit Claude Desktop from the system tray and reopen it.

Claude Desktop MCP logs live under:

```powershell
%APPDATA%\Claude\logs\
```

There is no configured test suite or linter yet. For now, use the MCP Inspector and small import/runtime smoke checks after changing server wiring or dependencies.

## Repository Layout

```text
D:\inversion
|-- README.md
|-- CLAUDE.md
|-- AGENTS.md
|-- sierra-mcp
|   |-- pyproject.toml
|   |-- uv.lock
|   |-- .env
|   `-- src\sierra_mcp
|       |-- server.py
|       |-- config.py
|       |-- dtc_client.py
|       |-- dtc_messages.py
|       `-- scid_reader.py
|-- discord-mcp
|   |-- pyproject.toml
|   |-- uv.lock
|   |-- .env
|   |-- .env.example
|   |-- groups.yml
|   `-- src\discord_mcp
|       |-- server.py
|       |-- config.py
|       |-- client.py
|       `-- groups.py
`-- journal-mcp
    |-- pyproject.toml
    |-- uv.lock
    |-- .env.example
    |-- data
    `-- src\journal_mcp
        |-- server.py
        |-- config.py
        `-- db.py
```

All `.env` files are local secrets/config and are gitignored.

## Sierra MCP Architecture

`sierra-mcp` exposes these tools:

- `ping_sierra`
- `get_quote`
- `get_recent_bars`
- `get_recent_bars_scid`
- `get_latest_tick_scid`
- `get_scid_status`
- `get_futures_context`
- `list_trade_accounts`
- `get_account_balance`
- `get_positions`

Key files:

- `server.py`: FastMCP tool definitions. Tools open a fresh `DTCClient` per call.
- `config.py`: loads `.env` from the project root or current working directory. Defaults include `SIERRA_DATA_PATH=D:\SierraChart\Data`.
- `dtc_client.py`: async TCP client for Sierra DTC JSON messages terminated by null bytes.
- `dtc_messages.py`: DTC message IDs.
- `scid_reader.py`: local `.scid` tick-file reader and bar aggregator.

Known Sierra state:

- `ping_sierra` works when Sierra Chart DTC is enabled.
- `get_recent_bars_scid` is validated and is the reliable market-data path today.
- `get_latest_tick_scid` and `get_scid_status` provide near-real-time last tick
  and file freshness from Sierra's local `.scid` file.
- `get_futures_context` is the main ES/NQ context pack. It uses `.scid`, filters
  an approximate current CME equity Globex session from 22:00 UTC, and returns
  latest tick, recent bars, VWAP, range, delta, ATR and simple bias.
- `get_quote` and DTC historical bars have returned `Request is not authorized` on the current Sierra/Denali setup. Do not assume DTC market data is fixed.
- `get_positions` uses `CURRENT_POSITIONS_REQUEST` (DTC type 305) and responds
  correctly with an empty list when there are no Sim positions.
- `get_account_balance` can return empty on Sim accounts; Sierra may not
  maintain account balances for Trade Simulation Mode accounts.

Sierra Chart local setup expected:

- DTC Protocol Server enabled.
- Encoding set to JSON.
- Trading port `11099`, historical port `11098`.
- For `.scid`, data files under `D:\SierraChart\Data`.

## Discord MCP Architecture

`discord-mcp` exposes these tools:

- `list_servers`
- `list_channels`
- `read_recent_messages`
- `search_messages`
- `fetch_image`
- `list_groups`
- `read_group`

Key files:

- `server.py`: FastMCP tools and message formatting.
- `client.py`: Discord REST API wrapper using a bot token.
- `config.py`: loads `DISCORD_BOT_TOKEN` from `.env`, overriding inherited shell env values.
- `groups.py`: loads and applies taxonomy rules.
- `groups.yml`: editable channel taxonomy.

Current Discord server:

- Server: `Estudio trading donAdri`
- Server ID: `1437840746567958632`

Current taxonomy:

- `prep`: pre-market watchlists.
- `post`: postmarket, setups, write-ups.
- `recursos`: playbook, resources, glossary, journal/backtest content.
- `fondeo`: funded-account content.
- `futuros`: futures course content.
- `stocks`: stocks course content.

Use `groups.yml` for channel taxonomy changes. Do not hardcode channel names in Python unless the behavior itself changes.

`fetch_image` returns Discord CDN image attachments as MCP image content. Discord CDN URLs expire, so fetch a fresh message first when images fail.

## Journal MCP Architecture

`journal-mcp` exposes these tools:

- `log_observation`
- `log_trade`
- `update_trade`
- `search_journal`
- `list_recent`
- `daily_summary`
- `list_workflows`
- `premarket_review`
- `postmarket_review`
- `weekly_trading_review`

It also exposes matching MCP prompts:

- `premarket_review`
- `postmarket_review`
- `weekly_trading_review`

Key files:

- `server.py`: FastMCP tools for writing and searching memory.
- `config.py`: loads optional `JOURNAL_DB_PATH`; default is `journal-mcp\data\journal.db`.
- `db.py`: SQLite schema and row serialization helpers.

The database is local and gitignored. It has two tables today: `observations`
and `trades`. Keep the schema simple until real Claude Desktop workflows show
what is missing. Prefer additive migrations when changing the schema after real
journal data exists.

Validated smoke path:

- create observation
- create trade
- close/update trade
- search by query/tag
- daily summary
- prompt text generation for premarket/postmarket/weekly workflows

## Windows And Path Notes

The repo path contains an accented character in the real local folder name. This has repeatedly broken module execution through wrappers (`python -m ...`, `mcp dev` default uv spawn, and Claude Desktop in some cases). The servers include a `sys.path` shim at the top of each `server.py`; direct file execution is the most reliable path.

When adding new MCP servers, include the same `sys.path` shim if they will be run as files by the Inspector or Claude Desktop.

## Git And Secrets

Never commit `.env`, virtualenvs, local databases, logs, or Sierra data files. The root `.gitignore` already excludes common Python env/cache files and `.env`.

Before commits:

```powershell
git -C D:\inversion status --short
git -C D:\inversion diff --stat
```

SQLite journal files under `journal-mcp\data` are gitignored. Do not commit
local journal databases or WAL/SHM files.

## Roadmap Guidance

Now that journal memory exists, useful vertical agents/prompts become practical
after the user has tried the journal in real Claude Desktop conversations:

- Prep analyst: reads Discord `prep` plus Sierra bars.
- Postmarket analyst: reads Discord `post`, screenshots, and journal outcomes.
- Risk officer: checks position size, funded-account rules, drawdown, and trade plan.
- Setup classifier: maps live ideas to donAdri playbook terms.

Do not build multi-agent orchestration before there is persistent memory and enough reliable data/tools. Start as MCP tools plus reusable prompts; promote to agents only when repeated workflows become stable.

## Practical Development Priorities

Prefer improvements that make Claude Desktop more useful immediately:

1. Make a tool reliable in the Inspector.
2. Wire it into Claude Desktop.
3. Document the example question it enables.
4. Only then expand the data surface.

For trading-related write tools, keep them out of scope until the read path and journal are stable. When order placement is eventually added, start with Sim only and require explicit per-call confirmation.
