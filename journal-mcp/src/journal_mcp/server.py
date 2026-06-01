import sys
from pathlib import Path
from typing import Any

# When loaded by `mcp dev` or Claude Desktop as a file, ensure the parent
# `src/` is on sys.path so journal_mcp.* imports resolve on Windows paths.
_src_dir = Path(__file__).resolve().parents[1]
if str(_src_dir) not in sys.path:
    sys.path.insert(0, str(_src_dir))

from mcp.server.fastmcp import FastMCP

from journal_mcp.config import Config
from journal_mcp.db import (
    connect,
    encode_json,
    matches_tags,
    normalize_tags,
    observation_to_dict,
    trade_to_dict,
    utc_now,
)

mcp = FastMCP("journal-mcp")


def _conn():
    return connect(Config.from_env().db_path)


def _clean_text(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


@mcp.prompt(
    name="premarket_review",
    title="Premarket Review",
    description="Build a futures premarket plan from Discord prep, Sierra context, and journal memory.",
)
def premarket_review(
    server: str = "Estudio trading donAdri",
    futures_symbol: str = "MESM26-CME",
    nasdaq_symbol: str = "MNQM26-CME",
) -> str:
    """Prompt Claude to run the premarket workflow across MCPs."""
    return f"""
Act as my futures premarket analyst.

Use the available MCP tools in this order:

1. Discord:
   - Read the `prep` group from server `{server}`.
   - Prioritize futures-related prep/watchlist content.

2. Sierra:
   - Call `get_futures_context` for `{futures_symbol}` with interval `1m`.
   - Call `get_futures_context` for `{nasdaq_symbol}` with interval `1m`.

3. Journal:
   - Search recent journal entries for tags: `premarket`, `plan`, `futures`, `risk`.
   - Look for recurring mistakes or rules that should affect today's plan.

Return:
- Market state for ES/MES and NQ/MNQ.
- Key levels and conditions from Discord prep.
- 3-5 scenarios for the session.
- Risk notes / things to avoid.
- A concise plan I can read before trading.

Then ask me whether to save the final plan to the journal. If I say yes, use
`log_observation` with tags `premarket`, `futures`, `plan`.
""".strip()


@mcp.prompt(
    name="postmarket_review",
    title="Postmarket Review",
    description="Review the trading day using Discord postmarket, Sierra context, screenshots, and journal memory.",
)
def postmarket_review(
    server: str = "Estudio trading donAdri",
    futures_symbol: str = "MESM26-CME",
    nasdaq_symbol: str = "MNQM26-CME",
) -> str:
    """Prompt Claude to run the postmarket workflow across MCPs."""
    return f"""
Act as my futures postmarket reviewer.

Use the available MCP tools in this order:

1. Discord:
   - Read the `post` group from server `{server}`.
   - If messages have important chart screenshots, fetch and inspect the images.

2. Sierra:
   - Call `get_futures_context` for `{futures_symbol}` with interval `1m`.
   - Call `get_futures_context` for `{nasdaq_symbol}` with interval `1m`.

3. Journal:
   - Search today's journal entries and recent entries tagged `mistake`,
     `review`, `risk`, `futures`, and `postmarket`.

Return:
- What actually happened in ES/MES and NQ/MNQ.
- Which Discord postmarket lessons/setups matter most.
- Whether the market respected or invalidated the premarket plan.
- My likely mistakes/opportunities based on journal history.
- 3 concrete lessons for tomorrow.

Then ask me whether to save a postmarket observation. If I say yes, use
`log_observation` with tags `postmarket`, `futures`, `review`.
""".strip()


@mcp.prompt(
    name="weekly_trading_review",
    title="Weekly Trading Review",
    description="Summarize the week using journal entries, Discord write-ups, and market context.",
)
def weekly_trading_review(
    server: str = "Estudio trading donAdri",
    futures_symbol: str = "MESM26-CME",
    nasdaq_symbol: str = "MNQM26-CME",
) -> str:
    """Prompt Claude to run a weekly review across MCPs."""
    return f"""
Act as my weekly trading reviewer.

Use the available MCP tools:

1. Journal:
   - Search this week's trades, observations, mistakes, and reviews.
   - Group findings by setup, risk, execution, psychology, and market context.

2. Discord:
   - Read the `post` group from `{server}`.
   - Read recent `writes-up` / weekly recap content if available.

3. Sierra:
   - Call `get_futures_context` for `{futures_symbol}` and `{nasdaq_symbol}`
     to anchor the current market regime.

Return:
- Weekly performance narrative.
- Best trade / worst trade / most repeated mistake.
- What the donAdri Discord content emphasized this week.
- What should change next week.
- 3 rules to keep visible during the next sessions.

Then ask me whether to save the weekly review. If I say yes, use
`log_observation` with tags `weekly-review`, `futures`, `process`.
""".strip()


@mcp.tool()
def log_observation(
    content: str,
    tags: list[str] | None = None,
    symbol: str = "",
    observed_at: str = "",
    source: str = "",
    metadata: dict[str, Any] | None = None,
) -> dict:
    """Write a dated observation to persistent memory.

    Use for market context, lessons, repeated mistakes, Discord-derived notes,
    or anything Claude should be able to recall later.

    content: The observation text.
    tags: Optional normalized labels like ["prep", "vwap", "mistake"].
    symbol: Optional symbol, e.g. MESM26-CME, NQ, MU.
    observed_at: Optional ISO timestamp/date for when it happened.
    source: Optional origin, e.g. discord:post, sierra, manual.
    metadata: Optional JSON object for extra structured details.
    """
    content = content.strip()
    if not content:
        return {"ok": False, "error": "content is required"}

    now = utc_now()
    with _conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO observations (
                created_at, observed_at, content, symbol, source, tags_json, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                now,
                _clean_text(observed_at),
                content,
                _clean_text(symbol.upper()),
                _clean_text(source),
                encode_json(normalize_tags(tags)),
                encode_json(metadata or {}),
            ),
        )
        row = conn.execute(
            "SELECT * FROM observations WHERE id = ?",
            (cur.lastrowid,),
        ).fetchone()

    return {"ok": True, "observation": observation_to_dict(row)}


@mcp.tool()
def log_trade(
    symbol: str,
    side: str,
    entry_price: float,
    quantity: float = 1.0,
    account: str = "",
    opened_at: str = "",
    setup: str = "",
    risk_amount: float | None = None,
    planned_r: float | None = None,
    notes: str = "",
    tags: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict:
    """Create a trade journal entry, open by default.

    Use this for planned, simulated, or real trades. Close/update it later with
    update_trade.
    """
    symbol = symbol.strip().upper()
    side_norm = side.strip().lower()
    if not symbol:
        return {"ok": False, "error": "symbol is required"}
    if side_norm not in {"long", "short", "buy", "sell"}:
        return {"ok": False, "error": "side must be long/short/buy/sell"}
    if quantity <= 0:
        return {"ok": False, "error": "quantity must be positive"}

    now = utc_now()
    with _conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO trades (
                created_at, updated_at, status, symbol, side, quantity, entry_price,
                account, opened_at, setup, risk_amount, planned_r, notes, tags_json,
                metadata_json
            ) VALUES (?, ?, 'open', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                now,
                now,
                symbol,
                side_norm,
                quantity,
                entry_price,
                _clean_text(account),
                _clean_text(opened_at),
                _clean_text(setup),
                risk_amount,
                planned_r,
                _clean_text(notes),
                encode_json(normalize_tags(tags)),
                encode_json(metadata or {}),
            ),
        )
        row = conn.execute("SELECT * FROM trades WHERE id = ?", (cur.lastrowid,)).fetchone()

    return {"ok": True, "trade": trade_to_dict(row)}


@mcp.tool()
def update_trade(
    trade_id: int,
    status: str = "",
    exit_price: float | None = None,
    closed_at: str = "",
    realized_r: float | None = None,
    notes: str = "",
    tags: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict:
    """Update or close an existing trade journal entry.

    Passing exit_price closes the trade unless status is explicitly set. Notes,
    tags and metadata replace the previous values only when provided.
    """
    now = utc_now()
    with _conn() as conn:
        existing = conn.execute("SELECT * FROM trades WHERE id = ?", (trade_id,)).fetchone()
        if existing is None:
            return {"ok": False, "error": f"trade not found: {trade_id}"}

        updates: list[str] = ["updated_at = ?"]
        values: list[Any] = [now]

        status_norm = status.strip().lower()
        if exit_price is not None and not status_norm:
            status_norm = "closed"
        if status_norm:
            if status_norm not in {"open", "closed", "cancelled"}:
                return {"ok": False, "error": "status must be open/closed/cancelled"}
            updates.append("status = ?")
            values.append(status_norm)
        if exit_price is not None:
            updates.append("exit_price = ?")
            values.append(exit_price)
        if closed_at.strip():
            updates.append("closed_at = ?")
            values.append(closed_at.strip())
        elif exit_price is not None:
            updates.append("closed_at = ?")
            values.append(now)
        if realized_r is not None:
            updates.append("realized_r = ?")
            values.append(realized_r)
        if notes.strip():
            updates.append("notes = ?")
            values.append(notes.strip())
        if tags is not None:
            updates.append("tags_json = ?")
            values.append(encode_json(normalize_tags(tags)))
        if metadata is not None:
            updates.append("metadata_json = ?")
            values.append(encode_json(metadata))

        values.append(trade_id)
        conn.execute(f"UPDATE trades SET {', '.join(updates)} WHERE id = ?", values)
        row = conn.execute("SELECT * FROM trades WHERE id = ?", (trade_id,)).fetchone()

    return {"ok": True, "trade": trade_to_dict(row)}


@mcp.tool()
def search_journal(
    query: str = "",
    tags: list[str] | None = None,
    symbol: str = "",
    since: str = "",
    until: str = "",
    kind: str = "all",
    limit: int = 50,
) -> dict:
    """Search observations and trades by text, tags, symbol and created date.

    kind: all, observations, or trades.
    since/until: ISO date or timestamp prefixes compared against created_at.
    """
    kind = kind.strip().lower()
    if kind not in {"all", "observations", "trades"}:
        return {"ok": False, "error": "kind must be all/observations/trades"}
    limit = max(1, min(limit, 500))
    needle = query.strip().lower()
    wanted_tags = normalize_tags(tags)
    symbol_norm = symbol.strip().upper()

    results: list[dict] = []
    with _conn() as conn:
        if kind in {"all", "observations"}:
            rows = conn.execute(
                "SELECT * FROM observations ORDER BY created_at DESC LIMIT ?",
                (limit * 5,),
            ).fetchall()
            for row in rows:
                item = observation_to_dict(row)
                text = " ".join(str(item.get(k) or "") for k in ("content", "source", "symbol"))
                if needle and needle not in text.lower():
                    continue
                if symbol_norm and item.get("symbol") != symbol_norm:
                    continue
                if since and item["created_at"] < since:
                    continue
                if until and item["created_at"] > until:
                    continue
                if not matches_tags(item["tags"], wanted_tags):
                    continue
                results.append(item)

        if kind in {"all", "trades"}:
            rows = conn.execute(
                "SELECT * FROM trades ORDER BY created_at DESC LIMIT ?",
                (limit * 5,),
            ).fetchall()
            for row in rows:
                item = trade_to_dict(row)
                text = " ".join(
                    str(item.get(k) or "")
                    for k in ("symbol", "side", "account", "setup", "notes", "status")
                )
                if needle and needle not in text.lower():
                    continue
                if symbol_norm and item.get("symbol") != symbol_norm:
                    continue
                if since and item["created_at"] < since:
                    continue
                if until and item["created_at"] > until:
                    continue
                if not matches_tags(item["tags"], wanted_tags):
                    continue
                results.append(item)

    results.sort(key=lambda x: x["created_at"], reverse=True)
    results = results[:limit]
    return {"ok": True, "count": len(results), "results": results}


@mcp.tool()
def list_recent(n: int = 20, kind: str = "all") -> dict:
    """List the most recent journal entries."""
    return search_journal(kind=kind, limit=n)


@mcp.tool()
def daily_summary(date: str = "") -> dict:
    """Return all journal entries for a date (YYYY-MM-DD, UTC by default).

    If date is empty, uses today's UTC date. This is the raw material for daily
    or weekly review conversations.
    """
    if not date:
        date = utc_now()[:10]
    return search_journal(since=date, until=f"{date}T99", limit=500)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
