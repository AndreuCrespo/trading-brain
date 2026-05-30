import os
from dataclasses import dataclass


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
        )
