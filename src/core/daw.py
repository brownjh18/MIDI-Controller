"""DAW selection and capability registry.

The phone app speaks one generic JSON schema.  A DAW is supported by pairing
the bridge with a companion script (or a control-surface adapter) that reads
``commands.json`` and writes ``state.json``.  This module only tracks which DAW
the user selected and what that DAW can do, so the app can show the right
controls and ignore state from a DAW that is not active.

File transport
--------------
Every scriptable DAW (Ableton, FL Studio, Reaper, Bitwig, ...) uses the same
two files in ``%TEMP%/AbletonBridgeMixer``:

    commands.json   app -> DAW   (written by core.commandfile)
    state.json      DAW -> app   (watched by core.statefile)

A state payload may carry ``"daw": "<id>"``.  Payloads without the field are
treated as Ableton, because that is the original adapter and its script predates
the field.  ``DawManager.matches`` is used by the relay so switching DAWs never
shows stale tracks from the previous one.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Order is the order shown in the app.
DAWS: dict[str, dict[str, Any]] = {
    "flstudio": {
        "name": "FL Studio",
        "capabilities": {
            "mixer": True,
            "devices": True,
            "clips": False,
            "transport": True,
            "exact_values": True,
            "banks": True,
        },
    },
    "logic": {
        "name": "Logic Pro",
        "capabilities": {
            "mixer": True,
            "devices": True,
            "clips": False,
            "transport": True,
            "exact_values": False,
            "banks": True,
        },
    },
    "studioone": {
        "name": "Studio One",
        "capabilities": {
            "mixer": True,
            "devices": True,
            "clips": False,
            "transport": True,
            "exact_values": False,
            "banks": True,
            # Studio One mixes through a send/broadcast bus layer, so the master
            # section gets one more fader (the listen bus) ahead of the master.
            "listen_bus": True,
        },
    },
    "cakewalk": {
        "name": "Cakewalk",
        "capabilities": {
            "mixer": True,
            "devices": True,
            "clips": False,
            "transport": True,
            "exact_values": False,
            "banks": True,
        },
    },
    "cubase": {
        "name": "Cubase",
        "capabilities": {
            "mixer": True,
            "devices": True,
            "clips": False,
            "transport": True,
            "exact_values": True,
            "banks": True,
        },
    },
    "reaper": {
        "name": "REAPER",
        "capabilities": {
            "mixer": True,
            "devices": True,
            "clips": False,
            "transport": True,
            "exact_values": True,
            "banks": True,
        },
    },
    "protools": {
        "name": "Pro Tools",
        "capabilities": {
            "mixer": True,
            "devices": True,
            "clips": False,
            "transport": True,
            "exact_values": False,
            "banks": True,
        },
    },
    "bitwig": {
        "name": "Bitwig Studio",
        "capabilities": {
            "mixer": True,
            "devices": True,
            "clips": True,
            "transport": True,
            "exact_values": True,
            "banks": True,
        },
    },
    "ableton": {
        "name": "Ableton Live",
        "capabilities": {
            "mixer": True,
            "devices": True,
            "clips": True,
            "transport": True,
            "exact_values": True,
            "banks": True,
        },
    },
}

DEFAULT_DAW = "ableton"

# Order shown in the app's picker. Ableton stays first because it is the
# original adapter and the default; the rest follow the requested roadmap.
DAW_ORDER = [
    "ableton",
    "flstudio",
    "logic",
    "studioone",
    "cakewalk",
    "cubase",
    "reaper",
    "protools",
    "bitwig",
]


def _config_path() -> str:
    base = os.environ.get("APPDATA") or os.path.expanduser("~/.config")
    return os.path.join(base, "MidiBridge", "config.json")


class DawManager:
    """Remembers the selected DAW and describes its capabilities."""

    def __init__(self, daw: Optional[str] = None) -> None:
        self._daw = DEFAULT_DAW
        self._config_path = _config_path()
        self._load()
        if daw and daw in DAWS:
            self._daw = daw

    @property
    def daw(self) -> str:
        return self._daw

    def set_daw(self, daw: str) -> bool:
        if daw not in DAWS:
            logger.warning("Unknown DAW selected: %s", daw)
            return False
        if daw == self._daw:
            return True
        self._daw = daw
        self._save()
        logger.info("Active DAW switched to %s", DAWS[daw]["name"])
        return True

    def matches(self, payload: dict[str, Any]) -> bool:
        """True when a state payload belongs to the selected DAW.

        Payloads without a ``daw`` field come from the original Ableton script.
        """
        source = payload.get("daw", "ableton")
        return source == self._daw

    def status(self) -> dict[str, Any]:
        profile = DAWS[self._daw]
        return {
            "daw": self._daw,
            "daw_name": profile["name"],
            "capabilities": profile["capabilities"],
            "daws": [
                {"id": key, "name": DAWS[key]["name"]}
                for key in DAW_ORDER
                if key in DAWS
            ],
        }

    # -- persistence -----------------------------------------------------

    def _load(self) -> None:
        try:
            with open(self._config_path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            selected = data.get("daw")
            if isinstance(selected, str) and selected in DAWS:
                self._daw = selected
        except (OSError, ValueError):
            pass

    def _save(self) -> None:
        try:
            os.makedirs(os.path.dirname(self._config_path), exist_ok=True)
            with open(self._config_path, "w", encoding="utf-8") as handle:
                json.dump({"daw": self._daw}, handle)
        except OSError as error:
            logger.warning("Could not save DAW selection: %s", error)
