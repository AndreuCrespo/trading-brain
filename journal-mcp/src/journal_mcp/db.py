import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def encode_json(value: Any) -> str:
    if value is None:
        value = [] if isinstance(value, list) else None
    return json.dumps(value, ensure_ascii=True)


def decode_json(value: str | None, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return fallback


def normalize_tags(tags: list[str] | None) -> list[str]:
    if not tags:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for tag in tags:
        cleaned = str(tag).strip().lower()
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            out.append(cleaned)
    return out


def connect(db_path: str) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    init_db(conn)
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS observations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            observed_at TEXT,
            content TEXT NOT NULL,
            symbol TEXT,
            source TEXT,
            tags_json TEXT NOT NULL DEFAULT '[]',
            metadata_json TEXT NOT NULL DEFAULT '{}'
        );

        CREATE INDEX IF NOT EXISTS idx_observations_created_at
            ON observations(created_at);
        CREATE INDEX IF NOT EXISTS idx_observations_symbol
            ON observations(symbol);

        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'open',
            symbol TEXT NOT NULL,
            side TEXT NOT NULL,
            quantity REAL NOT NULL,
            entry_price REAL NOT NULL,
            exit_price REAL,
            account TEXT,
            opened_at TEXT,
            closed_at TEXT,
            setup TEXT,
            risk_amount REAL,
            planned_r REAL,
            realized_r REAL,
            notes TEXT,
            tags_json TEXT NOT NULL DEFAULT '[]',
            metadata_json TEXT NOT NULL DEFAULT '{}'
        );

        CREATE INDEX IF NOT EXISTS idx_trades_created_at
            ON trades(created_at);
        CREATE INDEX IF NOT EXISTS idx_trades_symbol
            ON trades(symbol);
        CREATE INDEX IF NOT EXISTS idx_trades_status
            ON trades(status);

        CREATE TABLE IF NOT EXISTS knowledge_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            kind TEXT NOT NULL,
            topic TEXT NOT NULL,
            title TEXT NOT NULL,
            content TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'draft',
            confidence TEXT NOT NULL DEFAULT 'uncertain',
            source TEXT,
            source_ref TEXT,
            valid_from TEXT,
            valid_until TEXT,
            superseded_by INTEGER,
            tags_json TEXT NOT NULL DEFAULT '[]',
            metadata_json TEXT NOT NULL DEFAULT '{}',
            FOREIGN KEY (superseded_by) REFERENCES knowledge_items(id)
        );

        CREATE INDEX IF NOT EXISTS idx_knowledge_created_at
            ON knowledge_items(created_at);
        CREATE INDEX IF NOT EXISTS idx_knowledge_topic
            ON knowledge_items(topic);
        CREATE INDEX IF NOT EXISTS idx_knowledge_kind
            ON knowledge_items(kind);
        CREATE INDEX IF NOT EXISTS idx_knowledge_status
            ON knowledge_items(status);
        """
    )
    conn.commit()


def observation_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "type": "observation",
        "id": row["id"],
        "created_at": row["created_at"],
        "observed_at": row["observed_at"],
        "content": row["content"],
        "symbol": row["symbol"],
        "source": row["source"],
        "tags": decode_json(row["tags_json"], []),
        "metadata": decode_json(row["metadata_json"], {}),
    }


def trade_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "type": "trade",
        "id": row["id"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "status": row["status"],
        "symbol": row["symbol"],
        "side": row["side"],
        "quantity": row["quantity"],
        "entry_price": row["entry_price"],
        "exit_price": row["exit_price"],
        "account": row["account"],
        "opened_at": row["opened_at"],
        "closed_at": row["closed_at"],
        "setup": row["setup"],
        "risk_amount": row["risk_amount"],
        "planned_r": row["planned_r"],
        "realized_r": row["realized_r"],
        "notes": row["notes"],
        "tags": decode_json(row["tags_json"], []),
        "metadata": decode_json(row["metadata_json"], {}),
    }


def knowledge_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "type": "knowledge",
        "id": row["id"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "kind": row["kind"],
        "topic": row["topic"],
        "title": row["title"],
        "content": row["content"],
        "status": row["status"],
        "confidence": row["confidence"],
        "source": row["source"],
        "source_ref": row["source_ref"],
        "valid_from": row["valid_from"],
        "valid_until": row["valid_until"],
        "superseded_by": row["superseded_by"],
        "tags": decode_json(row["tags_json"], []),
        "metadata": decode_json(row["metadata_json"], {}),
    }


def matches_tags(item_tags: list[str], wanted_tags: list[str]) -> bool:
    if not wanted_tags:
        return True
    item = set(normalize_tags(item_tags))
    wanted = set(normalize_tags(wanted_tags))
    return wanted.issubset(item)
