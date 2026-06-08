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
    knowledge_to_dict,
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


KNOWLEDGE_KINDS = {
    "rule",
    "setup",
    "glossary",
    "example",
    "anti_example",
    "checklist",
    "open_question",
}
KNOWLEDGE_STATUSES = {"draft", "reviewed", "approved", "active", "stale", "deprecated"}
KNOWLEDGE_CONFIDENCES = {"explicit", "inferred", "uncertain"}


def _normalize_csv(value: str, allowed: set[str], default: set[str]) -> set[str]:
    if not value.strip():
        return default
    out = {part.strip().lower() for part in value.split(",") if part.strip()}
    invalid = out - allowed
    if invalid:
        raise ValueError(f"invalid values: {sorted(invalid)}")
    return out


@mcp.prompt(
    name="premarket_review",
    title="Premarket Review",
    description="Build a futures premarket plan from Discord prep, Sierra context, and journal memory.",
)
def premarket_review_prompt(
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
   - Call `get_market_features` for `{futures_symbol}` with interval `1m`.
   - Call `get_market_features` for `{nasdaq_symbol}` with interval `1m`.
   - If either `.scid` file is stale, say so clearly and avoid treating levels
     as current.
   - Use `get_futures_context` only as a compact secondary read if needed.

3. Journal:
   - Search recent journal entries for tags: `premarket`, `plan`, `futures`, `risk`.
   - Call `search_knowledge` for approved/active playbook entries with tags:
     `playbook`, `vwap`, `value-area`, `poc`, `dva`, `setup`.
   - Use approved/active knowledge to interpret current/previous VWAP, POC,
     VAH/VAL and Discord prep levels. Do not use draft/stale/deprecated items
     for decisions unless explicitly marked as context.

Return:
- Market state for ES/MES and NQ/MNQ.
- Structured indicator state: price vs VWAP, current VAH/VAL/POC, previous
  VAH/VAL/POC, delta and any divergence.
- Key levels and conditions from Discord prep.
- Which playbook rules apply today, and which do not.
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
def postmarket_review_prompt(
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
   - Call `get_market_features` for `{futures_symbol}` with interval `1m`.
   - Call `get_market_features` for `{nasdaq_symbol}` with interval `1m`.
   - Use `get_futures_context` only as a compact secondary read if needed.

3. Journal:
   - Search today's journal entries and recent entries tagged `mistake`,
     `review`, `risk`, `futures`, and `postmarket`.
   - Call `search_knowledge` for approved/active playbook entries relevant to
     VWAP/value/POC/delta/setup interpretation.

Return:
- What actually happened in ES/MES and NQ/MNQ.
- Whether price accepted/rejected VWAP, current value, previous value, POC,
  VAH/VAL, and how delta behaved.
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
def weekly_trading_review_prompt(
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
   - Call `get_market_features` for `{futures_symbol}` and `{nasdaq_symbol}`
     to anchor the current market regime with VWAP/value/POC/delta.

Return:
- Weekly performance narrative.
- Best trade / worst trade / most repeated mistake.
- What the donAdri Discord content emphasized this week.
- What should change next week.
- 3 rules to keep visible during the next sessions.

Then ask me whether to save the weekly review. If I say yes, use
`log_observation` with tags `weekly-review`, `futures`, `process`.
""".strip()


@mcp.prompt(
    name="discord_study_ingest",
    title="Discord Study Ingest",
    description="Turn Discord course messages and screenshots into structured playbook knowledge.",
)
def discord_study_ingest_prompt(
    server: str = "Estudio trading donAdri",
    group: str = "futuros",
    topic: str = "vwap value area setups",
) -> str:
    """Prompt Claude to extract reusable rules from Discord text/images."""
    return f"""
Act as my trading-system study assistant.

Goal: convert Discord course content into structured knowledge that can be
searched later during premarket/trading reviews. Focus topic: `{topic}`.

Use the available MCP tools in this order:

1. Discord:
   - Read the `{group}` group from server `{server}`.
   - Prioritize messages, embeds and attachments related to `{topic}`.
   - For relevant image attachments, call `fetch_image` and inspect the image.

2. Extraction:
   - Extract concrete rules, definitions, setup conditions, invalidation rules,
     examples, anti-examples and vocabulary.
   - Prefer structured statements over long prose.
   - Distinguish what is explicitly shown/taught from your inference.

3. Journal:
   - Save each useful concept with `add_knowledge_item`, not `log_observation`.
   - Default status should be `draft` until Andreu reviews it.
   - Use tags like `playbook`, `discord-study`, `futures`, plus topic-specific
     tags such as `vwap`, `value-area`, `poc`, `dva`, `delta`, `setup`.
   - Use confidence `explicit` for directly stated rules, `inferred` for your
     reconstruction, and `uncertain` for ambiguous vocabulary or hypotheses.
   - Include source/source_ref/metadata when available: server, group, channel,
     message_id, attachment filename/url.

Return:
- A concise study summary.
- A table of extracted rules/setups.
- Any ambiguous items that need human confirmation.
- Which draft knowledge items you saved and which should be reviewed/approved.
""".strip()


@mcp.tool()
def premarket_review(
    server: str = "Estudio trading donAdri",
    futures_symbol: str = "MESM26-CME",
    nasdaq_symbol: str = "MNQM26-CME",
) -> dict:
    """Return the premarket workflow instructions for Claude to execute.

    Claude Desktop currently exposes tools more reliably than MCP prompts. Call
    this tool, then follow the returned instructions using Discord, Sierra and
    Journal tools.
    """
    return {
        "ok": True,
        "workflow": "premarket_review",
        "instructions": premarket_review_prompt(server, futures_symbol, nasdaq_symbol),
    }


@mcp.tool()
def postmarket_review(
    server: str = "Estudio trading donAdri",
    futures_symbol: str = "MESM26-CME",
    nasdaq_symbol: str = "MNQM26-CME",
) -> dict:
    """Return the postmarket workflow instructions for Claude to execute."""
    return {
        "ok": True,
        "workflow": "postmarket_review",
        "instructions": postmarket_review_prompt(server, futures_symbol, nasdaq_symbol),
    }


@mcp.tool()
def weekly_trading_review(
    server: str = "Estudio trading donAdri",
    futures_symbol: str = "MESM26-CME",
    nasdaq_symbol: str = "MNQM26-CME",
) -> dict:
    """Return the weekly review workflow instructions for Claude to execute."""
    return {
        "ok": True,
        "workflow": "weekly_trading_review",
        "instructions": weekly_trading_review_prompt(server, futures_symbol, nasdaq_symbol),
    }


@mcp.tool()
def discord_study_ingest(
    server: str = "Estudio trading donAdri",
    group: str = "futuros",
    topic: str = "vwap value area setups",
) -> dict:
    """Return workflow instructions for turning Discord course content into journal knowledge."""
    return {
        "ok": True,
        "workflow": "discord_study_ingest",
        "instructions": discord_study_ingest_prompt(server, group, topic),
    }


@mcp.tool()
def list_workflows() -> dict:
    """List available trading workflows exposed by journal-mcp."""
    return {
        "ok": True,
        "workflows": [
            {
                "name": "premarket_review",
                "description": "Read Discord prep, Sierra market features and journal playbook/risk memory to build the daily plan.",
            },
            {
                "name": "postmarket_review",
                "description": "Read Discord postmarket/screenshots, Sierra market features and journal memory to review the day.",
            },
            {
                "name": "weekly_trading_review",
                "description": "Use journal, Discord write-ups and Sierra market features to summarize the week.",
            },
            {
                "name": "discord_study_ingest",
                "description": "Read Discord course messages/screenshots and save structured playbook knowledge.",
            },
        ],
    }


@mcp.tool()
def add_knowledge_item(
    kind: str,
    topic: str,
    title: str,
    content: str,
    tags: list[str] | None = None,
    status: str = "draft",
    confidence: str = "uncertain",
    source: str = "",
    source_ref: str = "",
    valid_from: str = "",
    valid_until: str = "",
    superseded_by: int | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict:
    """Add a structured playbook/knowledge item.

    Use this for Discord/PPT/video-derived trading-system knowledge that may
    later be reviewed and approved. Decision workflows should prefer approved
    or active items over draft/stale/deprecated items.
    """
    kind_norm = kind.strip().lower()
    status_norm = status.strip().lower() or "draft"
    confidence_norm = confidence.strip().lower() or "uncertain"
    topic_norm = topic.strip().lower()
    title = title.strip()
    content = content.strip()

    if kind_norm not in KNOWLEDGE_KINDS:
        return {"ok": False, "error": f"kind must be one of {sorted(KNOWLEDGE_KINDS)}"}
    if status_norm not in KNOWLEDGE_STATUSES:
        return {"ok": False, "error": f"status must be one of {sorted(KNOWLEDGE_STATUSES)}"}
    if confidence_norm not in KNOWLEDGE_CONFIDENCES:
        return {"ok": False, "error": f"confidence must be one of {sorted(KNOWLEDGE_CONFIDENCES)}"}
    if not topic_norm:
        return {"ok": False, "error": "topic is required"}
    if not title:
        return {"ok": False, "error": "title is required"}
    if not content:
        return {"ok": False, "error": "content is required"}

    now = utc_now()
    with _conn() as conn:
        if superseded_by is not None:
            existing = conn.execute(
                "SELECT id FROM knowledge_items WHERE id = ?",
                (superseded_by,),
            ).fetchone()
            if existing is None:
                return {"ok": False, "error": f"superseded_by not found: {superseded_by}"}

        cur = conn.execute(
            """
            INSERT INTO knowledge_items (
                created_at, updated_at, kind, topic, title, content, status,
                confidence, source, source_ref, valid_from, valid_until,
                superseded_by, tags_json, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                now,
                now,
                kind_norm,
                topic_norm,
                title,
                content,
                status_norm,
                confidence_norm,
                _clean_text(source),
                _clean_text(source_ref),
                _clean_text(valid_from),
                _clean_text(valid_until),
                superseded_by,
                encode_json(normalize_tags(tags)),
                encode_json(metadata or {}),
            ),
        )
        row = conn.execute(
            "SELECT * FROM knowledge_items WHERE id = ?",
            (cur.lastrowid,),
        ).fetchone()

    return {"ok": True, "knowledge": knowledge_to_dict(row)}


@mcp.tool()
def search_knowledge(
    query: str = "",
    tags: list[str] | None = None,
    topic: str = "",
    kind: str = "",
    statuses: str = "approved,active",
    include_historical: bool = False,
    limit: int = 50,
) -> dict:
    """Search structured playbook knowledge.

    By default this returns only approved/active knowledge for decision support.
    Set include_historical=true to include draft/reviewed/stale/deprecated items.
    statuses is a comma-separated filter such as "draft,reviewed" or
    "approved,active".
    """
    limit = max(1, min(limit, 500))
    needle = query.strip().lower()
    wanted_tags = normalize_tags(tags)
    topic_norm = topic.strip().lower()
    kind_norm = kind.strip().lower()
    if kind_norm and kind_norm not in KNOWLEDGE_KINDS:
        return {"ok": False, "error": f"kind must be one of {sorted(KNOWLEDGE_KINDS)}"}
    try:
        status_filter = _normalize_csv(
            statuses,
            KNOWLEDGE_STATUSES,
            {"approved", "active"},
        )
    except ValueError as exc:
        return {"ok": False, "error": f"statuses {exc}"}
    if include_historical:
        status_filter = KNOWLEDGE_STATUSES

    results: list[dict] = []
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM knowledge_items ORDER BY updated_at DESC, created_at DESC LIMIT ?",
            (limit * 5,),
        ).fetchall()
        for row in rows:
            item = knowledge_to_dict(row)
            text = " ".join(
                str(item.get(k) or "")
                for k in ("kind", "topic", "title", "content", "source", "source_ref", "status", "confidence")
            )
            if needle and needle not in text.lower():
                continue
            if topic_norm and item["topic"] != topic_norm:
                continue
            if kind_norm and item["kind"] != kind_norm:
                continue
            if item["status"] not in status_filter:
                continue
            if not matches_tags(item["tags"], wanted_tags):
                continue
            results.append(item)

    results = results[:limit]
    return {
        "ok": True,
        "count": len(results),
        "statuses": sorted(status_filter),
        "results": results,
    }


@mcp.tool()
def list_knowledge_topics(include_historical: bool = False) -> dict:
    """List knowledge topics with counts by status."""
    status_filter = KNOWLEDGE_STATUSES if include_historical else {"approved", "active"}
    with _conn() as conn:
        rows = conn.execute(
            """
            SELECT topic, status, COUNT(*) AS count
            FROM knowledge_items
            GROUP BY topic, status
            ORDER BY topic, status
            """
        ).fetchall()

    topics: dict[str, dict[str, Any]] = {}
    for row in rows:
        status = row["status"]
        if status not in status_filter:
            continue
        topic = row["topic"]
        bucket = topics.setdefault(topic, {"topic": topic, "count": 0, "statuses": {}})
        bucket["count"] += row["count"]
        bucket["statuses"][status] = row["count"]

    return {"ok": True, "count": len(topics), "topics": list(topics.values())}


@mcp.tool()
def review_knowledge_item(
    knowledge_id: int,
    status: str = "",
    confidence: str = "",
    valid_until: str = "",
    superseded_by: int | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict:
    """Review or update lifecycle fields for a knowledge item."""
    now = utc_now()
    status_norm = status.strip().lower()
    confidence_norm = confidence.strip().lower()
    if status_norm and status_norm not in KNOWLEDGE_STATUSES:
        return {"ok": False, "error": f"status must be one of {sorted(KNOWLEDGE_STATUSES)}"}
    if confidence_norm and confidence_norm not in KNOWLEDGE_CONFIDENCES:
        return {"ok": False, "error": f"confidence must be one of {sorted(KNOWLEDGE_CONFIDENCES)}"}

    with _conn() as conn:
        existing = conn.execute(
            "SELECT * FROM knowledge_items WHERE id = ?",
            (knowledge_id,),
        ).fetchone()
        if existing is None:
            return {"ok": False, "error": f"knowledge item not found: {knowledge_id}"}
        if superseded_by is not None:
            replacement = conn.execute(
                "SELECT id FROM knowledge_items WHERE id = ?",
                (superseded_by,),
            ).fetchone()
            if replacement is None:
                return {"ok": False, "error": f"superseded_by not found: {superseded_by}"}
            if superseded_by == knowledge_id:
                return {"ok": False, "error": "superseded_by cannot reference the same item"}

        updates: list[str] = ["updated_at = ?"]
        values: list[Any] = [now]
        if status_norm:
            updates.append("status = ?")
            values.append(status_norm)
        if confidence_norm:
            updates.append("confidence = ?")
            values.append(confidence_norm)
        if valid_until.strip():
            updates.append("valid_until = ?")
            values.append(valid_until.strip())
        if superseded_by is not None:
            updates.append("superseded_by = ?")
            values.append(superseded_by)
        if metadata is not None:
            updates.append("metadata_json = ?")
            values.append(encode_json(metadata))

        values.append(knowledge_id)
        conn.execute(f"UPDATE knowledge_items SET {', '.join(updates)} WHERE id = ?", values)
        row = conn.execute(
            "SELECT * FROM knowledge_items WHERE id = ?",
            (knowledge_id,),
        ).fetchone()

    return {"ok": True, "knowledge": knowledge_to_dict(row)}


@mcp.tool()
def promote_observation_to_knowledge(
    observation_id: int,
    kind: str,
    topic: str,
    title: str = "",
    status: str = "draft",
    confidence: str = "uncertain",
    tags: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict:
    """Promote an existing observation into a structured knowledge item.

    Useful for migrating early Discord-study observations into the reviewed
    playbook lifecycle without losing their original journal history.
    """
    kind_norm = kind.strip().lower()
    status_norm = status.strip().lower() or "draft"
    confidence_norm = confidence.strip().lower() or "uncertain"
    topic_norm = topic.strip().lower()
    if kind_norm not in KNOWLEDGE_KINDS:
        return {"ok": False, "error": f"kind must be one of {sorted(KNOWLEDGE_KINDS)}"}
    if status_norm not in KNOWLEDGE_STATUSES:
        return {"ok": False, "error": f"status must be one of {sorted(KNOWLEDGE_STATUSES)}"}
    if confidence_norm not in KNOWLEDGE_CONFIDENCES:
        return {"ok": False, "error": f"confidence must be one of {sorted(KNOWLEDGE_CONFIDENCES)}"}
    if not topic_norm:
        return {"ok": False, "error": "topic is required"}

    now = utc_now()
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM observations WHERE id = ?",
            (observation_id,),
        ).fetchone()
        if row is None:
            return {"ok": False, "error": f"observation not found: {observation_id}"}
        observation = observation_to_dict(row)

        merged_tags = normalize_tags([*(observation["tags"] or []), *(tags or [])])
        merged_metadata = {
            "promoted_from": {"type": "observation", "id": observation_id},
            "observation_metadata": observation.get("metadata") or {},
            **(metadata or {}),
        }
        cur = conn.execute(
            """
            INSERT INTO knowledge_items (
                created_at, updated_at, kind, topic, title, content, status,
                confidence, source, source_ref, valid_from, valid_until,
                superseded_by, tags_json, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                now,
                now,
                kind_norm,
                topic_norm,
                title.strip() or (observation["content"][:80] + ("..." if len(observation["content"]) > 80 else "")),
                observation["content"],
                status_norm,
                confidence_norm,
                observation.get("source"),
                f"observation:{observation_id}",
                observation.get("observed_at"),
                None,
                None,
                encode_json(merged_tags),
                encode_json(merged_metadata),
            ),
        )
        knowledge_row = conn.execute(
            "SELECT * FROM knowledge_items WHERE id = ?",
            (cur.lastrowid,),
        ).fetchone()

    return {
        "ok": True,
        "observation": observation,
        "knowledge": knowledge_to_dict(knowledge_row),
    }


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
