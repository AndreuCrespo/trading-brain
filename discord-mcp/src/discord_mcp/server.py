import logging
from urllib.parse import urlparse

import httpx
from mcp.server.fastmcp import FastMCP, Image

from discord_mcp.client import DiscordClient
from discord_mcp.config import Config

logging.basicConfig(level=logging.INFO)

mcp = FastMCP("discord-mcp")

# Discord channel types we care about as a knowledge source.
TEXT_CHANNEL_TYPES = {
    0: "text",
    5: "announcement",
    10: "announcement_thread",
    11: "public_thread",
    12: "private_thread",
    15: "forum",
}


def _format_message(m: dict) -> dict:
    return {
        "id": m.get("id"),
        "author": (m.get("author") or {}).get("username"),
        "author_id": (m.get("author") or {}).get("id"),
        "timestamp": m.get("timestamp"),
        "content": m.get("content"),
        "edited_timestamp": m.get("edited_timestamp"),
        "attachments": [
            {"url": a.get("url"), "filename": a.get("filename")}
            for a in (m.get("attachments") or [])
        ],
        "embeds": [
            {"title": e.get("title"), "description": e.get("description"), "url": e.get("url")}
            for e in (m.get("embeds") or [])
        ],
        "mentions": [u.get("username") for u in (m.get("mentions") or [])],
        "reactions": [
            {"emoji": (r.get("emoji") or {}).get("name"), "count": r.get("count")}
            for r in (m.get("reactions") or [])
        ],
    }


async def _resolve_guild_id(client: DiscordClient, server: str) -> str | None:
    """Accept either a numeric guild ID or a guild name (case-insensitive)."""
    if server.isdigit():
        return server
    guilds = await client.get_guilds()
    for g in guilds:
        if g.get("name", "").lower() == server.lower():
            return g.get("id")
    return None


async def _resolve_channel_id(client: DiscordClient, server: str, channel: str) -> str | None:
    """Accept either a numeric channel ID or a channel name within a given server."""
    if channel.isdigit():
        return channel
    guild_id = await _resolve_guild_id(client, server)
    if guild_id is None:
        return None
    channels = await client.get_channels(guild_id)
    needle = channel.lstrip("#").lower()
    for c in channels:
        if (c.get("name") or "").lower() == needle and c.get("type") in TEXT_CHANNEL_TYPES:
            return c.get("id")
    return None


@mcp.tool()
async def list_servers() -> dict:
    """List Discord servers (guilds) the bot is a member of.

    Returns id, name, and the bot's permissions there. Use the name or id to
    pass as 'server' to other tools.
    """
    config = Config.from_env()
    async with DiscordClient(config) as client:
        guilds = await client.get_guilds()
        return {
            "ok": True,
            "count": len(guilds),
            "servers": [
                {"id": g.get("id"), "name": g.get("name"), "owner": g.get("owner")}
                for g in guilds
            ],
        }


@mcp.tool()
async def list_channels(server: str) -> dict:
    """List text-readable channels (text, threads, announcements, forums) in a server.

    server: server name or numeric guild ID (use list_servers to discover).
    """
    config = Config.from_env()
    async with DiscordClient(config) as client:
        guild_id = await _resolve_guild_id(client, server)
        if guild_id is None:
            return {"ok": False, "error": f"server not found: {server}"}
        channels = await client.get_channels(guild_id)
        return {
            "ok": True,
            "server_id": guild_id,
            "count": sum(1 for c in channels if c.get("type") in TEXT_CHANNEL_TYPES),
            "channels": [
                {
                    "id": c.get("id"),
                    "name": c.get("name"),
                    "type": TEXT_CHANNEL_TYPES[c["type"]],
                    "parent_id": c.get("parent_id"),
                    "topic": c.get("topic"),
                }
                for c in channels
                if c.get("type") in TEXT_CHANNEL_TYPES
            ],
        }


@mcp.tool()
async def read_recent_messages(
    server: str,
    channel: str,
    limit: int = 50,
) -> dict:
    """Read the most recent messages from a channel.

    server: server name or guild ID.
    channel: channel name (with or without leading #) or channel ID.
    limit: number of messages (max 100). Most recent first.
    """
    config = Config.from_env()
    async with DiscordClient(config) as client:
        channel_id = await _resolve_channel_id(client, server, channel)
        if channel_id is None:
            return {"ok": False, "error": f"channel '{channel}' not found in '{server}'"}
        raw = await client.get_messages(channel_id, limit=limit)
        return {
            "ok": True,
            "channel_id": channel_id,
            "count": len(raw),
            "messages": [_format_message(m) for m in raw],
        }


@mcp.tool()
async def search_messages(
    server: str,
    channel: str,
    query: str,
    scan_limit: int = 300,
) -> dict:
    """Pull recent messages from a channel and filter client-side by substring match.

    Discord's bot REST API has no real search endpoint, so this fetches the last
    `scan_limit` messages (paginated, max ~1000) and filters them.

    server: server name or guild ID.
    channel: channel name or channel ID.
    query: case-insensitive substring to match in message content.
    scan_limit: how many recent messages to scan (default 300, hard cap 1000).
    """
    config = Config.from_env()
    scan_limit = max(1, min(scan_limit, 1000))
    needle = query.lower()

    async with DiscordClient(config) as client:
        channel_id = await _resolve_channel_id(client, server, channel)
        if channel_id is None:
            return {"ok": False, "error": f"channel '{channel}' not found in '{server}'"}

        collected: list[dict] = []
        before: str | None = None
        while len(collected) < scan_limit:
            page_size = min(100, scan_limit - len(collected))
            page = await client.get_messages(channel_id, limit=page_size, before=before)
            if not page:
                break
            collected.extend(page)
            before = page[-1].get("id")

        matches = [m for m in collected if needle in (m.get("content") or "").lower()]
        return {
            "ok": True,
            "channel_id": channel_id,
            "scanned": len(collected),
            "match_count": len(matches),
            "matches": [_format_message(m) for m in matches],
        }


_IMAGE_FORMATS = {
    ".png": "png",
    ".jpg": "jpeg",
    ".jpeg": "jpeg",
    ".gif": "gif",
    ".webp": "webp",
}


@mcp.tool()
async def fetch_image(url: str):
    """Download a Discord image attachment so it can be viewed inline.

    Use after read_recent_messages / search_messages when a message has an
    attachment whose URL ends in .png/.jpg/.gif/.webp and you need to actually
    see the image (chart screenshots, trade snapshots, etc.). Returns the raw
    image content embedded.

    Note: Discord CDN URLs are signed with an expiry (a few hours). Always fetch
    from a freshly-listed URL — don't reuse stale ones.
    """
    parsed = urlparse(url)
    if not parsed.hostname or not parsed.hostname.endswith(("discordapp.com", "discordapp.net")):
        return {"ok": False, "error": "URL must be from a Discord CDN host"}

    path = parsed.path.lower()
    fmt = next((v for ext, v in _IMAGE_FORMATS.items() if path.endswith(ext)), None)
    if fmt is None:
        return {"ok": False, "error": f"unsupported image format for path {path}"}

    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as http:
        r = await http.get(url)
    if r.status_code != 200:
        return {"ok": False, "error": f"HTTP {r.status_code} fetching image"}

    return Image(data=r.content, format=fmt)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
