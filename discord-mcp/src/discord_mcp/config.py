import os
from dataclasses import dataclass
from pathlib import Path

try:
    from dotenv import find_dotenv, load_dotenv

    # Try the project root next to src/ first, then walk up from CWD.
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
    bot_token: str
    api_base: str = "https://discord.com/api/v10"

    @classmethod
    def from_env(cls) -> "Config":
        token = os.getenv("DISCORD_BOT_TOKEN", "").strip()
        if not token:
            raise RuntimeError(
                "DISCORD_BOT_TOKEN env var not set. Create a bot at "
                "https://discord.com/developers/applications, enable Message "
                "Content Intent, and export the token."
            )
        return cls(bot_token=token)
