import httpx

from discord_mcp.config import Config


class DiscordError(Exception):
    pass


class DiscordClient:
    """Thin async wrapper over Discord's REST API using a bot token."""

    def __init__(self, config: Config):
        self.config = config
        self._http = httpx.AsyncClient(
            base_url=config.api_base,
            headers={"Authorization": f"Bot {config.bot_token}"},
            timeout=15,
        )

    async def close(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> "DiscordClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    async def _get(self, path: str, **params) -> list | dict:
        clean = {k: v for k, v in params.items() if v is not None}
        r = await self._http.get(path, params=clean)
        if r.status_code >= 400:
            raise DiscordError(f"{r.status_code} on {path}: {r.text}")
        return r.json()

    async def get_guilds(self) -> list[dict]:
        """Servers the bot is a member of."""
        return await self._get("/users/@me/guilds")  # type: ignore[return-value]

    async def get_channels(self, guild_id: str) -> list[dict]:
        """All channels in a guild (text, voice, threads, categories)."""
        return await self._get(f"/guilds/{guild_id}/channels")  # type: ignore[return-value]

    async def get_messages(
        self,
        channel_id: str,
        limit: int = 50,
        before: str | None = None,
        after: str | None = None,
    ) -> list[dict]:
        """Up to 100 messages from a channel, most recent first."""
        return await self._get(
            f"/channels/{channel_id}/messages",
            limit=min(100, max(1, limit)),
            before=before,
            after=after,
        )  # type: ignore[return-value]
