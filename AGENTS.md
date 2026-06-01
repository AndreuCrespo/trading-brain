# AGENTS.md

This repository is maintained with Codex and Claude Code. Use the root `CLAUDE.md` as the canonical operating guide for commands, architecture, known limitations, and roadmap.

Quick start:

```powershell
cd D:\inversion\sierra-mcp
uv sync
uv run mcp dev src/sierra_mcp/server.py

cd D:\inversion\discord-mcp
uv sync
uv run mcp dev src/discord_mcp/server.py

cd D:\inversion\journal-mcp
uv sync
uv run mcp dev src/journal_mcp/server.py
```

Important local detail: the old repo path contained an accented character; direct file execution is still preferred in Claude Desktop and the MCP Inspector rather than `python -m ...`.
