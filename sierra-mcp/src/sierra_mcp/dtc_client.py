import asyncio
import json
import logging
from typing import Any, Iterable

from sierra_mcp.config import Config
from sierra_mcp.dtc_messages import LogonResult, MessageType

logger = logging.getLogger(__name__)

PROTOCOL_VERSION = 8


class DTCError(Exception):
    pass


class DTCClient:
    def __init__(self, host: str, port: int, config: Config):
        self.host = host
        self.port = port
        self.config = config
        self.logon_response: dict[str, Any] | None = None
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._heartbeat_task: asyncio.Task | None = None
        self._reader_task: asyncio.Task | None = None
        self._subscribers: dict[int, asyncio.Queue[dict[str, Any]]] = {}

    async def connect(self) -> dict[str, Any]:
        self._reader, self._writer = await asyncio.open_connection(self.host, self.port)
        self._reader_task = asyncio.create_task(self._read_loop())

        response = await self.request(
            {
                "Type": int(MessageType.LOGON_REQUEST),
                "ProtocolVersion": PROTOCOL_VERSION,
                "Username": self.config.username,
                "Password": self.config.password,
                "HeartbeatIntervalInSeconds": self.config.heartbeat_interval,
                "ClientName": self.config.client_name,
            },
            [MessageType.LOGON_RESPONSE],
            timeout=10,
        )

        if response.get("Result") != LogonResult.SUCCESS:
            raise DTCError(
                f"Logon failed (Result={response.get('Result')}): "
                f"{response.get('ResultText', 'unknown')}"
            )

        self.logon_response = response
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        return response

    async def close(self) -> None:
        for task in (self._heartbeat_task, self._reader_task):
            if task is not None:
                task.cancel()
        if self._writer is not None:
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except Exception:
                pass

    def subscribe(self, msg_type: int) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._subscribers[int(msg_type)] = queue
        return queue

    def unsubscribe(self, msg_type: int) -> None:
        self._subscribers.pop(int(msg_type), None)

    async def send(self, msg: dict[str, Any]) -> None:
        if self._writer is None:
            raise DTCError("not connected")
        payload = json.dumps(msg).encode("utf-8") + b"\x00"
        self._writer.write(payload)
        await self._writer.drain()

    async def request(
        self,
        send_msg: dict[str, Any],
        response_types: Iterable[int],
        timeout: float = 10,
    ) -> dict[str, Any]:
        """Send a message and wait for the first response of any given type.

        Use for single-response request/response patterns. For multi-message
        responses (positions, historical bars) subscribe directly and loop.
        """
        types = [int(t) for t in response_types]
        queues = {t: self.subscribe(t) for t in types}
        try:
            await self.send(send_msg)
            tasks = {asyncio.create_task(q.get()): t for t, q in queues.items()}
            done, pending = await asyncio.wait(
                tasks.keys(), timeout=timeout, return_when=asyncio.FIRST_COMPLETED
            )
            for p in pending:
                p.cancel()
            if not done:
                raise asyncio.TimeoutError(
                    f"no response (waiting on types {types}) within {timeout}s"
                )
            return done.pop().result()
        finally:
            for t in types:
                self.unsubscribe(t)

    async def _read_loop(self) -> None:
        assert self._reader is not None
        try:
            while True:
                raw = await self._reader.readuntil(b"\x00")
                msg = json.loads(raw[:-1].decode("utf-8"))
                msg_type = msg.get("Type")
                queue = self._subscribers.get(msg_type)
                if queue is not None:
                    await queue.put(msg)
                elif msg_type != MessageType.HEARTBEAT:
                    logger.debug("unhandled message: %s", msg)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("read loop terminated")

    async def _heartbeat_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(self.config.heartbeat_interval)
                await self.send({"Type": int(MessageType.HEARTBEAT)})
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("heartbeat loop terminated")
