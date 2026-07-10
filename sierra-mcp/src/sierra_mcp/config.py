import json
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


def _load_allowed_sim_accounts() -> tuple[str, ...]:
    candidates = [
        Path(__file__).resolve().parents[2] / "safety.json",
        Path.cwd() / "safety.json",
    ]
    for path in candidates:
        if not path.exists():
            continue
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        accounts = data.get("allowed_sim_accounts", [])
        if not isinstance(accounts, list):
            raise ValueError("safety.json allowed_sim_accounts must be a list")
        return tuple(str(account).strip() for account in accounts if str(account).strip())
    return ()


@dataclass(frozen=True)
class Config:
    host: str
    trading_port: int
    historical_port: int
    username: str
    password: str
    client_name: str
    heartbeat_interval: int
    data_path: str
    allowed_sim_accounts: tuple[str, ...]
    # Max age of the last local SCID tick before order tools refuse to open new
    # sim positions. Default 900s tolerates the ~10-minute Trading Evaluator
    # delayed feed while still blocking closed-market/dead-feed submissions.
    order_max_tick_age_seconds: int

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            host=os.getenv("SIERRA_DTC_HOST", "127.0.0.1"),
            trading_port=int(os.getenv("SIERRA_DTC_PORT", "11099")),
            historical_port=int(os.getenv("SIERRA_DTC_HISTORICAL_PORT", "11098")),
            username=os.getenv("SIERRA_DTC_USERNAME", ""),
            password=os.getenv("SIERRA_DTC_PASSWORD", ""),
            client_name=os.getenv("SIERRA_DTC_CLIENT_NAME", "sierra-mcp"),
            heartbeat_interval=int(os.getenv("SIERRA_DTC_HEARTBEAT", "10")),
            data_path=os.getenv("SIERRA_DATA_PATH", r"D:\SierraChart\Data"),
            allowed_sim_accounts=_load_allowed_sim_accounts(),
            order_max_tick_age_seconds=int(
                os.getenv("SIERRA_ORDER_MAX_TICK_AGE_SECONDS", "900")
            ),
        )
