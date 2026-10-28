"""Mirrors a hardware keyboard's pitch bend and mod wheel to the app.

The controller app shows a pitch wheel and a mod wheel for the selected
channel. Those are driven by the performer's hands on the real keyboard, so
this module watches a physical MIDI input and pushes every change over the
WebSocket as a ``wheels`` message.

Only pitch bend and the mod wheel (CC 1) are relayed; notes stay between the
keyboard and the DAW. Virtual cables (loopMIDI, teVirtualMIDI) are skipped so
the bridge never echoes its own outgoing MIDI back to the app.
"""

import logging
import os
import threading
import time
from typing import Callable, Dict, List, Optional, Tuple

from . import winmidi

logger = logging.getLogger(__name__)

#: Ports that are not a real keyboard: our own loopback, virtual cables and
#: the Wavetable synth's input.
VIRTUAL_HINTS = (
    "loopmidi",
    "tevirtualmidi",
    "virtualmidi",
    "midi-bridge",
    "abletonbridge",
    "internal midi",
    "microsoft gs",
    "wavetable",
)

#: Shortest gap between two pushes for the same channel, in seconds. A moving
#: pitch wheel can emit hundreds of messages per second.
MIN_INTERVAL = 0.02


def _is_virtual(name: str) -> bool:
    lowered = name.lower()
    return any(hint in lowered for hint in VIRTUAL_HINTS)


def _pick(devices: List[Tuple[int, str]], preferred: Optional[str]) -> Optional[Tuple[int, str]]:
    """Chooses the keyboard to watch: explicit name first, then any real port."""
    real = [d for d in devices if not _is_virtual(d[1])]
    if preferred:
        for index, name in devices:
            if name == preferred:
                return index, name
    if real:
        return real[0]
    return None


class KeyboardWatcher:
    """Watches a physical MIDI input and reports pitch/mod wheel positions.

    ``on_change(channel, pitch, mod)`` is called from a background thread;
    ``pitch`` and ``mod`` are 0..127 (64 = centred) and only the field that
    changed is non-``None``.
    """

    def __init__(
        self,
        on_change: Callable[[int, Optional[int], Optional[int]], None],
        on_device: Optional[Callable[[Optional[str]], None]] = None,
    ) -> None:
        self._on_change = on_change
        self._on_device = on_device
        self._preferred = os.environ.get("MIDI_INPUT_PORT") or None
        self._input: Optional[winmidi.WinMmInput] = None
        self._name: Optional[str] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._pitch: Dict[int, int] = {}
        self._mod: Dict[int, int] = {}
        self._last_sent: Dict[int, float] = {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def start(self) -> None:
        """Starts the watcher thread (which also handles hot-plugging)."""
        self._thread = threading.Thread(
            target=self._run, name="keyboard-watcher", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        """Stops the watcher and releases the device."""
        self._stop.set()
        self.close()

    def close(self) -> None:
        """Closes the currently open input device, if any."""
        current, self._input, self._name = self._input, None, None
        if current is not None:
            try:
                current.close()
            except Exception as error:
                logger.warning("Could not close keyboard input: %s", error)
            self._notify_device()

    def _notify_device(self) -> None:
        """Tells the bridge which keyboard (if any) is being watched."""
        if self._on_device is None:
            return
        try:
            self._on_device(self._name)
        except Exception as error:
            logger.warning("Keyboard device notification failed: %s", error)

    @property
    def name(self) -> Optional[str]:
        """Name of the port being watched, or ``None`` when no keyboard is seen."""
        return self._name

    # ------------------------------------------------------------------
    # Device handling
    # ------------------------------------------------------------------
    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._refresh()
            except Exception as error:
                logger.warning("Keyboard watcher error: %s", error)
            self._stop.wait(2.0)

    def _refresh(self) -> None:
        """Opens the keyboard when it appears and reopens it after a replug."""
        devices = winmidi.get_input_devices()
        if self._input is not None and any(
            name == self._name for _index, name in devices
        ):
            return
        if self._input is not None:
            logger.info("Keyboard '%s' disconnected.", self._name)
            self.close()
        choice = _pick(devices, self._preferred)
        if choice is None:
            return
        index, name = choice
        self._input = winmidi.WinMmInput(index, name, self._handle)
        self._name = name
        logger.info("Listening to keyboard '%s' for pitch bend and mod.", name)
        self._notify_device()

    # ------------------------------------------------------------------
    # Incoming messages
    # ------------------------------------------------------------------
    def _handle(self, status: int, data1: int, data2: int) -> None:
        """Extracts pitch bend / mod wheel from a short message."""
        kind = status & 0xF0
        channel = status & 0x0F

        if kind == 0xE0:  # pitch bend, 14-bit
            bend = data1 | (data2 << 7)
            value = int(round(bend / 16383.0 * 127.0))
            if value == self._pitch.get(channel):
                return
            self._pitch[channel] = value
            self._emit(channel, value, None)
        elif kind == 0xB0 and data1 == 1:  # mod wheel
            if data2 == self._mod.get(channel):
                return
            self._mod[channel] = data2
            self._emit(channel, None, data2)

    def _emit(self, channel: int, pitch: Optional[int], mod: Optional[int]) -> None:
        """Rate-limits and forwards one wheel change."""
        now = time.time()
        if now - self._last_sent.get(channel, 0.0) < MIN_INTERVAL:
            return
        self._last_sent[channel] = now
        try:
            self._on_change(channel, pitch, mod)
        except Exception as error:
            logger.warning("Keyboard push failed: %s", error)
