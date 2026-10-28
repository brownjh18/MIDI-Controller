"""App -> Live command channel.

The app's control changes normally travel as MIDI CCs on the loopMIDI port.
When that loopback is not carrying data (virtual port driver asleep, port
grabbed by another client, ...) the bridge also drops the same absolute
targets into a small JSON file that the remote script applies on its next
tick. Absolute values only, so a dropped or duplicated write can never leave
Live in the wrong place.

Target ids understood by the script:

    track:0 .. track:7   channel-strip volume / pan
    return:0, return:1   the reverb / delay return tracks
    master               the master fader
    metronome            Live's metronome on/off
"""

import json
import logging
import os
import tempfile
import threading
import time
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

COMMAND_DIR = os.path.join(tempfile.gettempdir(), "AbletonBridgeMixer")
COMMAND_FILE = os.path.join(COMMAND_DIR, "commands.json")

MAX_STRIP_CHANNEL = 7
MAIN_FX_CHANNEL = 8  # zero-based -> MIDI channel 9
# CC 91 = reverb send, CC 94 = delay send, CC 7 = master volume.
MAIN_FX_TARGETS = {91: "return:0", 94: "return:1", 7: "master"}

_lock = threading.Lock()
_pending: Dict[str, Dict[str, Any]] = {}
_sequence = 0


def _target_for(channel: int, control: int) -> Tuple[Optional[str], Optional[str]]:
    """Maps a CC to (target id, field) or (None, None) when it is not ours."""
    if 0 <= channel <= MAX_STRIP_CHANNEL:
        if control == 7:
            return "track:%d" % channel, "volume"
        if control == 10:
            return "track:%d" % channel, "pan"
        return None, None
    if channel == MAIN_FX_CHANNEL:
        target = MAIN_FX_TARGETS.get(control)
        if target is not None:
            return target, "volume"
    return None, None


def push_control_change(channel: int, control: int, value: int) -> None:
    """Queues one CC as an absolute target for the remote script."""
    target, field = _target_for(channel, control)
    if target is None:
        return

    clamped = max(0, min(127, int(value)))
    if field == "pan":
        amount = round((clamped - 64) / 64.0, 6)
    else:
        amount = round(clamped / 127.0, 6)

    with _lock:
        _store(target, field, amount)


def push_metronome(enabled: bool) -> None:
    """Queues a metronome on/off change for the remote script."""
    with _lock:
        _store("metronome", "value", bool(enabled))


def push_transport(action: str) -> None:
    """Queues a transport start/stop for the remote script."""
    with _lock:
        _store("transport", "value", "start" if action == "start" else "stop")


def push_tempo(bpm: float) -> None:
    """Queues a tempo change for the remote script."""
    with _lock:
        _store("tempo", "value", round(float(bpm), 4))


def _next_sequence() -> int:
    """Monotonic sequence that survives a bridge restart.

    Live remembers the last sequence it applied per target, so the counter must
    keep growing across processes — wall-clock milliseconds do that.
    """
    global _sequence
    now = int(time.time() * 1000)
    _sequence = now if now > _sequence else _sequence + 1
    return _sequence


def _store(target: str, field: str, amount: Any) -> None:
    entry = _pending.setdefault(target, {})
    entry[field] = {"value": amount, "seq": _next_sequence()}
    _write()


def _write() -> None:
    try:
        os.makedirs(COMMAND_DIR, exist_ok=True)
        temp_path = COMMAND_FILE + ".tmp"
        with open(temp_path, "w", encoding="utf-8") as handle:
            json.dump(_pending, handle)
        _replace_with_retry(temp_path, COMMAND_FILE)
    except OSError as error:
        logger.warning("Command file write failed: %s", error)


def _replace_with_retry(source: str, target: str, attempts: int = 4) -> None:
    """Swaps the file in place, retrying while Live holds it open.

    Windows refuses ``os.replace`` when the destination is being read by the
    remote script, so a couple of very short retries keep fader moves intact.
    """
    for attempt in range(attempts):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.01)
