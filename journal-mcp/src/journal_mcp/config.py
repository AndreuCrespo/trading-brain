import os
from dataclasses import dataclass
from pathlib import Path

try:
    from dotenv import find_dotenv, load_dotenv

    _candidates = [
        Path(__file__).resolve().parents[2] / ".env",
        Path.cwd() / ".env",
    ]
    for _p in _candidates:
        if _p.exists():
            load_dotenv(_p, override=True)
            break
    else:
        _found = find_dotenv(usecwd=True)
        if _found:
            load_dotenv(_found, override=True)
except ImportError:
    pass


@dataclass(frozen=True)
class Config:
    db_path: str

    @classmethod
    def from_env(cls) -> "Config":
        default_path = Path(__file__).resolve().parents[2] / "data" / "journal.db"
        return cls(db_path=os.getenv("JOURNAL_DB_PATH", str(default_path)))
