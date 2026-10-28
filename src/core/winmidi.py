"""Windows MIDI output fallback using the winmm API through ctypes.

The bridge normally sends MIDI through mido + python-rtmidi. When that native
backend is not installed (for example on a stock Microsoft Store Python), this
module drives the same workstation / virtual ports through the WinMM API that
ships with Windows, so the controller keeps working without extra installs.

Only short messages are supported (CC, notes, pitch wheel, clock and transport),
which is exactly what the mobile client emits.
"""

import ctypes
import logging
from ctypes import wintypes
from typing import Any, Callable, List, Tuple

logger = logging.getLogger(__name__)

MMSYSERR_NOERROR = 0
MAXPNAMELEN = 32


class MIDIOUTCAPS(ctypes.Structure):
    """MIDIOUTCAPSW device capabilities block."""

    _fields_ = [
        ("wMid", wintypes.WORD),
        ("wPid", wintypes.WORD),
        ("vDriverVersion", wintypes.DWORD),
        ("szPname", wintypes.WCHAR * MAXPNAMELEN),
        ("wTechnology", wintypes.WORD),
        ("wVoices", wintypes.WORD),
        ("wNotes", wintypes.WORD),
        ("wChannelMask", wintypes.WORD),
        ("dwSupport", wintypes.DWORD),
    ]


class MIDIINCAPS(ctypes.Structure):
    """MIDIINCAPSW device capabilities block."""

    _fields_ = [
        ("wMid", wintypes.WORD),
        ("wPid", wintypes.WORD),
        ("vDriverVersion", wintypes.DWORD),
        ("szPname", wintypes.WCHAR * MAXPNAMELEN),
        ("dwSupport", wintypes.DWORD),
    ]


# midiInOpen flags / callback message ids
CALLBACK_FUNCTION = 0x00030000
MIM_DATA = 0x3C3

#: Callback Windows uses for streamed short messages:
#: (handle, message, instance, param1, param2).
MIDIINPROC = ctypes.WINFUNCTYPE(
    None,
    wintypes.HANDLE,
    wintypes.UINT,
    ctypes.c_size_t,
    ctypes.c_size_t,
    ctypes.c_size_t,
)


def _load_winmm() -> Any:
    """Loads winmm and declares the entry points used here."""
    winmm = ctypes.WinDLL("winmm")
    winmm.midiOutGetNumDevs.restype = wintypes.UINT
    winmm.midiOutGetDevCapsW.argtypes = [
        wintypes.UINT,
        ctypes.POINTER(MIDIOUTCAPS),
        wintypes.UINT,
    ]
    winmm.midiOutGetDevCapsW.restype = wintypes.UINT
    winmm.midiOutOpen.argtypes = [
        ctypes.POINTER(wintypes.HANDLE),
        wintypes.UINT,
        ctypes.c_size_t,  # DWORD_PTR
        ctypes.c_size_t,
        wintypes.DWORD,
    ]
    winmm.midiOutOpen.restype = wintypes.UINT
    winmm.midiOutShortMsg.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    winmm.midiOutShortMsg.restype = wintypes.UINT
    winmm.midiOutClose.argtypes = [wintypes.HANDLE]
    winmm.midiOutClose.restype = wintypes.UINT
    winmm.midiInGetNumDevs.restype = wintypes.UINT
    winmm.midiInGetDevCapsW.argtypes = [
        wintypes.UINT,
        ctypes.POINTER(MIDIINCAPS),
        wintypes.UINT,
    ]
    winmm.midiInGetDevCapsW.restype = wintypes.UINT
    winmm.midiInOpen.argtypes = [
        ctypes.POINTER(wintypes.HANDLE),
        wintypes.UINT,
        ctypes.c_size_t,  # DWORD_PTR: callback address
        ctypes.c_size_t,  # DWORD_PTR: instance data
        wintypes.DWORD,
    ]
    winmm.midiInOpen.restype = wintypes.UINT
    winmm.midiInStart.argtypes = [wintypes.HANDLE]
    winmm.midiInStart.restype = wintypes.UINT
    winmm.midiInStop.argtypes = [wintypes.HANDLE]
    winmm.midiInStop.restype = wintypes.UINT
    winmm.midiInReset.argtypes = [wintypes.HANDLE]
    winmm.midiInReset.restype = wintypes.UINT
    winmm.midiInClose.argtypes = [wintypes.HANDLE]
    winmm.midiInClose.restype = wintypes.UINT
    return winmm


def get_input_devices() -> List[Tuple[int, str]]:
    """Lists the MIDI input devices Windows exposes as (index, name) pairs."""
    winmm = _load_winmm()
    devices: List[Tuple[int, str]] = []
    caps = MIDIINCAPS()
    for index in range(int(winmm.midiInGetNumDevs())):
        if (
            winmm.midiInGetDevCapsW(index, ctypes.byref(caps), ctypes.sizeof(caps))
            == MMSYSERR_NOERROR
        ):
            devices.append((index, caps.szPname))
    return devices


def get_input_names() -> List[str]:
    """Lists the MIDI input device names Windows exposes."""
    return [name for _index, name in get_input_devices()]


def get_output_names() -> List[str]:
    """Lists the MIDI output devices Windows exposes through winmm."""
    winmm = _load_winmm()
    names: List[str] = []
    caps = MIDIOUTCAPS()
    for index in range(int(winmm.midiOutGetNumDevs())):
        if (
            winmm.midiOutGetDevCapsW(index, ctypes.byref(caps), ctypes.sizeof(caps))
            == MMSYSERR_NOERROR
        ):
            names.append(caps.szPname)
    return names


class WinMmOutput:
    """A MIDI output port backed by a winmm device handle."""

    def __init__(self, index: int, name: str) -> None:
        self.name = name
        self._winmm = _load_winmm()
        self._handle = wintypes.HANDLE()
        result = self._winmm.midiOutOpen(
            ctypes.byref(self._handle), index, 0, 0, 0
        )
        if result != MMSYSERR_NOERROR:
            raise OSError(f"midiOutOpen failed for '{name}' (error {result})")

    def send(self, message: Any) -> None:
        """Sends a short message; accepts anything exposing .bytes() (mido)."""
        if not self._handle:
            return
        data = message.bytes()
        if not data or len(data) > 3:
            return
        packed = 0
        for position, byte in enumerate(data):
            packed |= byte << (8 * position)
        result = self._winmm.midiOutShortMsg(self._handle, packed)
        if result != MMSYSERR_NOERROR:
            logger.warning(
                "midiOutShortMsg failed for '%s' (error %s): %s",
                self.name,
                result,
                list(data),
            )

    def close(self) -> None:
        """Closes the winmm device handle."""
        if self._handle:
            self._winmm.midiOutClose(self._handle)
            self._handle = wintypes.HANDLE()
            logger.info("WinMM MIDI port '%s' closed.", self.name)


class WinMmInput:
    """A MIDI *input* port backed by a winmm device handle.

    The hardware keyboard streams short messages; each one is handed to
    ``on_message(status, data1, data2)`` from the WinMM thread, so callers must
    not block there.
    """

    def __init__(
        self,
        index: int,
        name: str,
        on_message: Callable[[int, int, int], None],
    ) -> None:
        self.name = name
        self._winmm = _load_winmm()
        self._on_message = on_message
        self._handle = wintypes.HANDLE()
        # Windows keeps the raw address; the Python object must stay alive.
        self._callback = MIDIINPROC(self._dispatch)
        result = self._winmm.midiInOpen(
            ctypes.byref(self._handle),
            index,
            ctypes.cast(self._callback, ctypes.c_void_p).value,
            0,
            CALLBACK_FUNCTION,
        )
        if result != MMSYSERR_NOERROR:
            raise OSError(f"midiInOpen failed for '{name}' (error {result})")
        result = self._winmm.midiInStart(self._handle)
        if result != MMSYSERR_NOERROR:
            self._winmm.midiInClose(self._handle)
            raise OSError(f"midiInStart failed for '{name}' (error {result})")

    def _dispatch(self, _handle: Any, message: int, _instance: Any, param1: Any, _param2: Any) -> None:
        """WinMM callback: unpacks short messages and forwards them."""
        if message != MIM_DATA:
            return
        status = int(param1) & 0xFF
        data1 = (int(param1) >> 8) & 0xFF
        data2 = (int(param1) >> 16) & 0xFF
        try:
            self._on_message(status, data1, data2)
        except Exception:
            logger.exception("MIDI input callback failed for '%s'", self.name)

    def close(self) -> None:
        """Stops and closes the input device."""
        if self._handle:
            self._winmm.midiInStop(self._handle)
            self._winmm.midiInReset(self._handle)
            self._winmm.midiInClose(self._handle)
            self._handle = wintypes.HANDLE()
            logger.info("WinMM MIDI input '%s' closed.", self.name)


class VirtualMidiPort:
    """A self-hosted virtual MIDI port (loopMIDI / teVirtualMIDI driver API).

    While this process holds the port open, it appears to other applications as
    a normal MIDI *input* — which is how a DAW such as Ableton Live receives the
    controller without any extra MIDI cabling or third-party cable software.
    """

    # Driver notification callback: (handle, command, data, length, param).
    _NOTIFY = ctypes.WINFUNCTYPE(
        None,
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.LPVOID,
    )
    _TX_RX = 0x03  # LOOPMIDI_FLAG_SUPPORTS_TX | SUPPORTS_RX

    def __init__(self, name: str) -> None:
        dll_name = (
            "teVirtualMIDI64.dll"
            if ctypes.sizeof(ctypes.c_void_p) == 8
            else "teVirtualMIDI.dll"
        )
        self.name = name
        self._dll = ctypes.WinDLL(dll_name)
        self._dll.virtualMIDICreatePort.argtypes = [
            wintypes.LPCWSTR,
            self._NOTIFY,
            wintypes.DWORD,
            wintypes.LPVOID,
        ]
        self._dll.virtualMIDICreatePort.restype = wintypes.HANDLE
        self._dll.virtualMIDISendData.argtypes = [
            wintypes.HANDLE,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        self._dll.virtualMIDISendData.restype = wintypes.BOOL
        self._dll.virtualMIDIClosePort.argtypes = [wintypes.HANDLE]
        # The callback must outlive every send, so keep it on the instance.
        self._callback = self._NOTIFY(self._on_event)
        self._handle = self._dll.virtualMIDICreatePort(
            name, self._callback, self._TX_RX, None
        )
        if not self._handle:
            raise OSError(f"virtualMIDICreatePort failed for '{name}'")

    @staticmethod
    def _on_event(handle, command, data, length, param) -> None:
        """Driver notification sink (a client opened or closed the port)."""

    def send(self, message: Any) -> None:
        """Sends a short message; accepts anything exposing .bytes() (mido)."""
        if not self._handle:
            return
        payload = message.bytes()
        if not payload or len(payload) > 3:
            return
        buffer = (ctypes.c_ubyte * len(payload))(*payload)
        self._dll.virtualMIDISendData(self._handle, buffer, len(payload))

    def close(self) -> None:
        """Releases the virtual port, removing it from the system."""
        if self._handle:
            self._dll.virtualMIDIClosePort(self._handle)
            self._handle = None
            logger.info("Virtual MIDI port '%s' released.", self.name)
