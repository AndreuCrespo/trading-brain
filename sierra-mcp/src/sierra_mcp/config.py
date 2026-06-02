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
    host: str
    trading_port: int
    historical_port: int
    username: str
    password: str
    client_name: str
    heartbeat_interval: int
    data_path: str
    allowed_sim_accounts: tuple[str, ...]

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
            allowed_sim_accounts=tuple(
                account.strip()
                for account in os.getenv("SIERRA_ALLOWED_SIM_ACCOUNTS", "").split(",")
                if account.strip()
            ),
        )
