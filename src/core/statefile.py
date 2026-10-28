"""File-drop transport for the AbletonBridgeMixer remote script.

Live's embedded Python omits the ``idna`` codec, which makes every socket
name lookup fail with ``unknown encoding: idna``. The remote script therefore
writes each set snapshot to a small JSON file as well; this watcher relays new
snapshots to the WebSocket clients, so the app mirrors the set even when the
loopback socket cannot be opened.
"""

import asyncio
import json
import logging
import os
import tempfile
import time
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger(__name__)

STATE_DIR = os.path.join(tempfile.gettempdir(), "AbletonBridgeMixer")
STATE_FILE = os.path.join(STATE_DIR, "state.json")
POLL_SECONDS = 0.5
# Snapshots are re-sent even when nothing changed. The app ignores Live for a
# moment after the performer moves a control (so a stale echo cannot fight the
# drag); without a repeat, that one discarded frame would leave the fader a
# step off until Live happens to change again.
REFRESH_SECONDS = 1.0


class StateFileWatcher:
    """Polls the remote-script state file and relays changed snapshots."""

    def __init__(self, on_message: Callable[[dict[str, Any]], Awaitable[None]]) -> None:
        self._on_message = on_message
        self._task: Optional[asyncio.Task[None]] = None
        self._last_stamp: Optional[tuple[int, int]] = None
        self._last_payload: Optional[dict[str, Any]] = None
        self._last_emit = 0.0

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run())
        logger.info("Watching remote-script state file at %s", STATE_FILE)

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _run(self) -> None:
        while True:
            try:
                emitted = False
                if os.path.exists(STATE_FILE):
                    stat = os.stat(STATE_FILE)
                    stamp = (stat.st_mtime_ns, stat.st_size)
                    if stamp != self._last_stamp:
                        self._last_stamp = stamp
                        with open(STATE_FILE, "r", encoding="utf-8") as handle:
                            data = json.load(handle)
                        if isinstance(data, dict):
                            self._last_payload = data
                            self._last_emit = time.monotonic()
                            emitted = True
                            await self._on_message(data)
                if not emitted and self._last_payload is not None:
                    if time.monotonic() - self._last_emit >= REFRESH_SECONDS:
                        self._last_emit = time.monotonic()
                        await self._on_message(self._last_payload)
            except (OSError, ValueError) as error:
                logger.warning("State file read failed: %s", error)
            await asyncio.sleep(POLL_SECONDS)
