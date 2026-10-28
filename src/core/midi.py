import mido
import logging
import os
from typing import Any, Optional

from core import commandfile

logger = logging.getLogger(__name__)
DEFAULT_PORT_NAME = "AbletonBridge"
MIXER_PORT_SUFFIX = " Mixer"


class MidiInterface:
    """Send MIDI messages to an existing workstation-facing MIDI port."""

    def __init__(self, port_name: Optional[str] = None):
        self.port_name = (port_name or os.environ.get("MIDI_OUTPUT_PORT") or DEFAULT_PORT_NAME).strip()
        # Every message is mirrored to all open ports. Normally that is a single
        # device; when the bridge hosts its own virtual ports it holds both the
        # main port (notes for Ableton tracks) and the mixer port (CC consumed
        # by the AbletonBridgeMixer remote script).
        self._output_ports: list[Any] = []
        self._available_outputs: list[str] = []
        self.availability_error: Optional[str] = None

    @property
    def available_outputs(self) -> list[str]:
        return list(self._available_outputs)

    @property
    def is_open(self) -> bool:
        return bool(self._output_ports)

    def open(self) -> bool:
        """
        Opens the virtual MIDI output port.

        Returns True only when the configured workstation port was opened.
        When python-rtmidi is unavailable, falls back to the Windows winmm API.
        """
        try:
            self._available_outputs = mido.get_output_names()
        except (ImportError, OSError, RuntimeError) as error:
            return self._open_with_winmm(error)

        matching_port = next(
            (
                name
                for name in self._available_outputs
                if name.strip().casefold() == self.port_name.casefold()
            ),
            None,
        )
        if matching_port is None:
            self.availability_error = (
                f"MIDI output '{self.port_name}' was not found. Available outputs: "
                f"{', '.join(self._available_outputs) or '(none)'}"
            )
            logger.error(
                "%s",
                self.availability_error,
            )
            self._output_ports = []
            return False

        try:
            self._output_ports = [mido.open_output(matching_port)]
            self.port_name = matching_port
            self.availability_error = None
            logger.info("Connected to MIDI output '%s'.", matching_port)
            return True
        except (ImportError, OSError, RuntimeError) as error:
            self.availability_error = f"Could not open MIDI output '{matching_port}': {error}"
            logger.exception("%s", self.availability_error)
            self._output_ports = []
            return False

    def _open_with_winmm(self, reason: Exception) -> bool:
        """
        Falls back to the Windows winmm API when python-rtmidi is unavailable.

        Args:
            reason (Exception): The mido/rtmidi failure that triggered this path.

        Returns True when a matching Windows MIDI output was opened.
        """
        if os.name != "nt":
            self._available_outputs = []
            self.availability_error = (
                "Could not enumerate MIDI ports. Install python-rtmidi and "
                f"configure a MIDI backend: {reason}"
            )
            logger.error("%s", self.availability_error)
            self._output_ports = []
            return False

        try:
            from core import winmidi  # Windows-only, ctypes based
        except Exception as error:
            self._available_outputs = []
            self.availability_error = (
                "Could not load the winmm MIDI backend "
                f"({reason}); fallback failed too: {error}"
            )
            logger.error("%s", self.availability_error)
            self._output_ports = []
            return False

        try:
            names = winmidi.get_output_names()
        except Exception as error:
            self._available_outputs = []
            self.availability_error = (
                "Could not enumerate Windows MIDI ports: "
                f"{error} (python-rtmidi unavailable: {reason})"
            )
            logger.error("%s", self.availability_error)
            self._output_ports = []
            return False

        self._available_outputs = names
        matching_port = next(
            (
                name
                for name in names
                if name.strip().casefold() in {
                    self.port_name.casefold(),
                    f"{self.port_name}{MIXER_PORT_SUFFIX}".casefold(),
                }
            ),
            None,
        )
        # A name with the mixer suffix means "host the virtual pair".
        if matching_port and matching_port.strip().casefold().endswith(
            MIXER_PORT_SUFFIX.casefold()
        ):
            return self._host_virtual_ports(winmidi)

        if matching_port is None:
            # Nothing on the machine matches the configured port, so host it as
            # a virtual port that a DAW (Ableton, FL, ...) can listen to.
            return self._host_virtual_ports(winmidi)

        try:
            self._output_ports = [
                winmidi.WinMmOutput(names.index(matching_port), matching_port)
            ]
            self.port_name = matching_port
            self.availability_error = None
            logger.info(
                "Connected to MIDI output '%s' via winmm (python-rtmidi unavailable: %s).",
                matching_port,
                reason,
            )
            return True
        except Exception as error:
            self.availability_error = f"Could not open MIDI output '{matching_port}': {error}"
            logger.exception("%s", self.availability_error)
            self._output_ports = []
            return False

    def _host_virtual_ports(self, winmidi: Any) -> bool:
        """
        Hosts the configured port name as self-contained virtual MIDI ports.

        Two ports are created and every message is mirrored to both:

        - ``<name>``        - the main port. A DAW enables it as a MIDI input
          with "Track" on, so notes and CC reach the instruments.
        - ``<name> Mixer``  - the mixer port. The AbletonBridgeMixer remote
          script is attached to it and converts CC 7 / CC 10 on channels 1-8
          into live track volume and pan changes.

        Args:
            winmidi (Any): The loaded Windows MIDI backend module.

        Returns True when at least the main virtual port was created.
        """
        wanted = [self.port_name, f"{self.port_name}{MIXER_PORT_SUFFIX}"]
        opened: list[Any] = []
        errors: list[str] = []
        for name in wanted:
            try:
                opened.append(winmidi.VirtualMidiPort(name))
            except Exception as error:
                errors.append(f"'{name}': {error}")

        if not opened:
            self.availability_error = (
                f"MIDI output '{self.port_name}' was not found and hosting virtual ports "
                f"failed: {'; '.join(errors)}"
            )
            logger.error("%s", self.availability_error)
            self._output_ports = []
            return False

        self._output_ports = opened
        if self.port_name not in self._available_outputs:
            self._available_outputs.append(self.port_name)
        self.availability_error = None
        logger.info(
            "Hosting virtual MIDI port%s %s. Enable the main port in your DAW's MIDI "
            "input (Ableton: Preferences > Link/Tempo/MIDI).",
            "s" if len(opened) > 1 else "",
            ", ".join(f"'{port.name}'" for port in opened),
        )
        return True

    def close(self) -> None:
        """
        Closes every open MIDI output port.
        """
        for port in self._output_ports:
            try:
                port.close()
                logger.info("MIDI port '%s' closed.", port.name)
            except Exception:
                logger.exception("Error closing MIDI port '%s'.", getattr(port, "name", "?"))
        self._output_ports = []

    def _broadcast(self, message: Any) -> None:
        """Sends one MIDI message to every open port."""
        for port in self._output_ports:
            port.send(message)

    def send_control_change(self, control: int, value: int, channel: int = 0) -> None:
        """
        Sends a MIDI Control Change message.

        Args:
            control (int): Standard MIDI CC parameter number (0-127).
            value (int): Data value (0-127).
            channel (int): MIDI channel (0-15).
        """
        if not self._output_ports:
            return

        try:
            self._broadcast(
                mido.Message('control_change', channel=channel, control=control, value=value)
            )
            logger.debug(
                "CC out: ch=%s cc=%s value=%s ports=%s",
                channel + 1,
                control,
                value,
                [getattr(p, "name", "?") for p in self._output_ports],
            )
        except Exception as e:
            logger.exception("Error sending MIDI CC: %s", e)

        # File fallback so fader moves still reach Live when the loopback
        # MIDI port is not carrying data.
        try:
            commandfile.push_control_change(channel, control, value)
        except Exception as error:
            logger.warning("Command file push failed: %s", error)

    def send_pitch_bend(self, value: int, channel: int = 0) -> None:
        """Sends a pitch bend value expressed on the controller's 0-127 scale."""
        if not self._output_ports:
            return

        pitch = (
            round(value / 64 * 8192) - 8192
            if value < 64
            else round((value - 64) / 63 * 8191)
        )
        try:
            self._broadcast(
                mido.Message('pitchwheel', channel=channel, pitch=pitch)
            )
        except Exception as e:
            logger.exception("Error sending MIDI pitch bend: %s", e)

    def send_note_on(self, note: int, velocity: int = 100, channel: int = 0) -> None:
        """
        Sends a MIDI Note On message.

        Args:
            note (int): Note number (0-127).
            velocity (int): Velocity (0-127).
            channel (int): MIDI channel (0-15).
        """
        if not self._output_ports:
            return

        try:
            self._broadcast(
                mido.Message('note_on', channel=channel, note=note, velocity=velocity)
            )
        except Exception as e:
            logger.exception("Error sending MIDI Note On: %s", e)

    def send_note_off(self, note: int, channel: int = 0) -> None:
        """
        Sends a MIDI Note Off message.

        Args:
            note (int): Note number (0-127).
            channel (int): MIDI channel (0-15).
        """
        if not self._output_ports:
            return

        try:
            self._broadcast(
                mido.Message('note_off', channel=channel, note=note, velocity=0) # Typically velocity 0
            )
        except Exception as e:
            logger.exception("Error sending MIDI Note Off: %s", e)

    def send_clock(self) -> None:
        """
        Sends a MIDI Timing Clock pulse (24 per quarter note).

        The client streams these at the current tempo; the workstation slaves
        its transport to the incoming clock.
        """
        if not self._output_ports:
            return

        try:
            self._broadcast(mido.Message('clock'))
        except Exception as e:
            logger.exception("Error sending MIDI clock: %s", e)

    def send_transport(self, kind: str) -> None:
        """
        Sends a MIDI transport message.

        Args:
            kind (str): One of 'start', 'continue' or 'stop'.
        """
        if not self._output_ports or kind not in ("start", "continue", "stop"):
            return

        try:
            self._broadcast(mido.Message(kind))
        except Exception as e:
            logger.exception("Error sending MIDI transport '%s': %s", kind, e)

    def set_output(self, name: str) -> bool:
        """
        Switches the active MIDI output port.

        Args:
            name (str): Name of the workstation port to open.

        Returns True when the new port was opened successfully.
        """
        self.close()
        self.port_name = (name or "").strip() or DEFAULT_PORT_NAME
        return self.open()

if __name__ == "__main__":
    # Example usage for testing purposes
    import time
    
    print("Listing output ports:", mido.get_output_names())
    
    midi = MidiInterface(port_name='TestBridge')
    if midi.open():
        print("Sending test messages...")
        # Send CC volume (CC 7) example
        midi.send_control_change(control=7, value=64)
        time.sleep(0.5)
        midi.send_control_change(control=7, value=127)
        time.sleep(0.5)
        
        # Send note example
        midi.send_note_on(note=60, velocity=100) # C4
        time.sleep(1)
        midi.send_note_off(note=60)
        
        midi.close()
