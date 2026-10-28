"""Loopback TCP link for the AbletonBridgeMixer remote script.

Live's control-surface sandbox pushes newline-delimited JSON over a plain
local socket (e.g. the names of the tracks the mixer is controlling); the
bridge relays every message to its WebSocket clients so the phone app can
mirror the Live set in real time.
"""

import asyncio
import json
import logging
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger(__name__)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8767


class ScriptLink:
    """Asyncio TCP server receiving messages from the Ableton remote script."""

    def __init__(
        self,
        on_message: Callable[[dict[str, Any]], Awaitable[None]],
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
    ) -> None:
        self.host = host
        self.port = port
        self._on_message = on_message
        self._server: Optional[asyncio.Server] = None

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        peer = writer.get_extra_info("peername")
        logger.info("Ableton script connected: %s", peer)
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                try:
                    data = json.loads(line)
                except (json.JSONDecodeError, UnicodeDecodeError) as error:
                    logger.warning("Ignoring bad script message: %s", error)
                    continue
                if isinstance(data, dict):
                    try:
                        await self._on_message(data)
                    except Exception as error:
                        logger.error("Error relaying script message: %s", error)
        except (ConnectionResetError, asyncio.IncompleteReadError) as error:
            logger.info("Ableton script connection error: %s", error)
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
            logger.info("Ableton script disconnected: %s", peer)

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._handle_client, self.host, self.port
        )
        logger.info("Ableton script link listening on %s:%s", self.host, self.port)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
            logger.info("Ableton script link stopped.")
