import asyncio
import json
import logging
from typing import Any, Optional

import mido
import websockets

from core import commandfile, winmidi
from core.daw import DawManager
from core.midi import MidiInterface

logging.basicConfig(level=logging.INFO)
logger: logging.Logger = logging.getLogger("WSServer")

class WebSocketServer:
    """Manages WebSocket server to receive control data from mobile."""

    def __init__(
        self,
        host: str,
        port: int,
        midi_handler: MidiInterface,
        daw_manager: Optional[DawManager] = None,
        on_daw_changed: Optional[Any] = None,
    ) -> None:
        self.host: str = host
        self.port: int = port
        self.midi_handler: MidiInterface = midi_handler
        self.daw_manager: Optional[DawManager] = daw_manager
        # Called with the DAW id after a switch so the bridge can re-point its
        # MIDI output and start/stop the Studio One surface mirror.
        self.on_daw_changed: Optional[Any] = on_daw_changed
        # Called with (kind, slot) when the app taps an insert/send row; the
        # Studio One mirror asks the surface to focus that device so its
        # parameters stream back for the detail panel.
        self.on_device_request: Optional[Any] = None
        self._shutdown_event: asyncio.Event = asyncio.Event()
        self._clients: set[Any] = set()
        self._relayed: dict[str, Any] = {}
        self._s1_output: Optional[Any] = None
        self._s1_opened = False

    def _status_payload(self) -> dict[str, Any]:
        """Builds the status message sent to clients on connect and on demand."""
        payload: dict[str, Any] = {
            "type": "status",
            "midi_available": self.midi_handler.is_open,
            "midi_output": self.midi_handler.port_name,
            "available_outputs": self.midi_handler.available_outputs,
            "midi_error": self.midi_handler.availability_error,
        }
        if self.daw_manager is not None:
            payload.update(self.daw_manager.status())
        return payload

    def clear_state(self) -> None:
        """Drops cached DAW state so a switch never shows the old set."""
        self._relayed.pop("mixer_state", None)

    # -- Studio One output (loopMIDI) ---------------------------------------
    #
    # The phone always sends the Ableton wire format (CC = fader/pan,
    # notes for toggles) and the Jarvis Bridge surface speaks it exactly,
    # so the Studio One route is a plain reroute of every app message to
    # loopMIDI instead of AbletonBridge.

    _S1_OUTPUT_NAME = "loopMIDI Port"
    _S1_PLAY = 0x60
    _S1_STOP = 0x61
    _S1_RECORD = 0x62
    _S1_CLICK = 0x63

    def _is_studioone(self) -> bool:
        return self.daw_manager is not None and self.daw_manager.daw == "studioone"

    def _s1_out(self) -> Optional[Any]:
        if self._s1_opened and self._s1_output is None:
            return None  # known missing
        if self._s1_output is not None:
            return self._s1_output
        self._s1_opened = True
        try:
            names = winmidi.get_output_names()
            wanted = self._S1_OUTPUT_NAME.lower()
            match = next((i for i, name in enumerate(names) if name.lower() == wanted), None)
            if match is None:
                logger.warning(
                    "Studio One: MIDI output '%s' not found among %s; "
                    "create a loopMIDI port named exactly '%s'",
                    self._S1_OUTPUT_NAME, names or "(none)", self._S1_OUTPUT_NAME,
                )
                return None
            self._s1_output = winmidi.WinMmOutput(match, self._S1_OUTPUT_NAME)
            logger.info("Studio One: using MIDI output '%s' (device %d)", self._S1_OUTPUT_NAME, match)
            return self._s1_output
        except OSError as error:
            logger.warning("Studio One: could not open loopMIDI output: %s", error)
            return None

    def _s1_send(self, message: Any) -> None:
        out = self._s1_out()
        if out is None:
            return
        try:
            out.send(message)
        except Exception as error:
            logger.exception("Studio One: send failed: %s", error)

    def _s1_note_on(self, note: int, velocity: int = 100) -> None:
        self._s1_send(mido.Message("note_on", channel=0, note=note, velocity=velocity))

    def _s1_note_off(self, note: int) -> None:
        self._s1_send(mido.Message("note_off", channel=0, note=note, velocity=0))

    def _s1_control_change(self, control: int, value: int, channel: int) -> None:
        self._s1_send(mido.Message("control_change", channel=channel, control=control, value=value))

    async def _handle_connection(self, websocket: Any) -> None:
        """Processes incoming WebSocket messages."""
        logger.info(f"Client connected from {websocket.remote_address}")
        self._clients.add(websocket)
        
        try:
            await websocket.send(json.dumps(self._status_payload()))
            # Replay cached server pushes (track names) so late joiners
            # are current even after an app or bridge reconnect.
            for cached in self._relayed.values():
                await websocket.send(json.dumps(cached))
            async for message in websocket:
                try:
                    data = json.loads(message)
                    if not isinstance(data, dict):
                        logger.warning("Ignoring non-object WebSocket message: %s", message)
                        continue

                    msg_type = data.get("type")

                    # Connection maintenance messages carry no MIDI channel.
                    if msg_type == "ping":
                        await websocket.send(json.dumps({"type": "pong", "t": data.get("t")}))
                        continue

                    if msg_type == "clock":
                        self.midi_handler.send_clock()
                        continue

                    if msg_type in ("start", "continue", "stop"):
                        if self._is_studioone():
                            self._s1_note_on(
                                self._S1_STOP if msg_type == "stop" else self._S1_PLAY
                            )
                        else:
                            self.midi_handler.send_transport(msg_type)
                            commandfile.push_transport(
                                "stop" if msg_type == "stop" else "start"
                            )
                        continue

                    if msg_type == "tempo":
                        tempo = data.get("value")
                        if isinstance(tempo, (int, float)) and not isinstance(
                            tempo, bool
                        ):
                            commandfile.push_tempo(float(tempo))
                        continue

                    if msg_type == "device_param":
                        track = data.get("track")
                        device = data.get("device", 0)
                        param = data.get("param")
                        value = data.get("value")
                        if (
                            type(track) is int
                            and type(device) is int
                            and type(param) is int
                            and isinstance(value, (int, float))
                        ):
                            commandfile.push_device_param(
                                track, device, param, float(value)
                            )
                        continue

                    if msg_type == "select_track":
                        index = data.get("index", data.get("value"))
                        if isinstance(index, int) and 0 <= index <= 127:
                            if self._is_studioone() and index < 15:
                                # MIDI path: CC 60+index on ch8 hits the surface's
                                # momentary Toggle selectRequest[i] -> selectValue$ch.
                                self._s1_control_change(60 + index, 127, 8)
                            else:
                                commandfile.push_select_track(index)
                        continue

                    if msg_type == "select_device":
                        kind = data.get("kind", "insert")
                        slot = data.get("slot")
                        if (
                            isinstance(kind, str)
                            and kind in ("insert", "send")
                            and type(slot) is int
                            and 0 <= slot < 8
                        ):
                            if self.on_device_request is not None:
                                try:
                                    self.on_device_request(kind, slot)
                                except Exception:
                                    logger.exception(
                                        "Device request failed for %s %d",
                                        kind,
                                        slot,
                                    )
                            elif self._is_studioone():
                                # Fallback: CC 20+i opens an insert, 28+i a send.
                                base = 28 if kind == "send" else 20
                                self._s1_control_change(base + slot, 127, 8)
                        continue

                    if msg_type == "clip":
                        track = data.get("track")
                        slot = data.get("slot", 0)
                        action = data.get("action")
                        if (
                            type(track) is int
                            and type(slot) is int
                            and isinstance(action, str)
                        ):
                            commandfile.push_clip(track, slot, action)
                        continue

                    if msg_type == "metronome":
                        if self._is_studioone():
                            self._s1_note_on(self._S1_CLICK, velocity=100 if bool(data.get("value")) else 0)
                        else:
                            commandfile.push_metronome(bool(data.get("value")))
                        continue

                    if msg_type == "record":
                        if self._is_studioone():
                            self._s1_note_on(self._S1_RECORD, velocity=100 if bool(data.get("value")) else 0)
                        else:
                            commandfile.push_record(bool(data.get("value")))
                        continue

                    if msg_type == "select_output":
                        selected = data.get("name")
                        if isinstance(selected, str) and selected.strip():
                            self.midi_handler.set_output(selected)
                            await websocket.send(json.dumps(self._status_payload()))
                        continue

                    if msg_type == "set_daw":
                        selected = data.get("daw", data.get("value"))
                        if isinstance(selected, str) and self.daw_manager is not None:
                            changed = selected != self.daw_manager.daw
                            if self.daw_manager.set_daw(selected):
                                if changed:
                                    # A different DAW means the cached tracks
                                    # are meaningless; drop them before
                                    # re-announcing.
                                    self.clear_state()
                                    if self.on_daw_changed is not None:
                                        try:
                                            self.on_daw_changed(selected)
                                        except Exception:
                                            logger.exception(
                                                "DAW switch hook failed for %s",
                                                selected,
                                            )
                                await self.broadcast(self._status_payload())
                        continue

                    control = data.get("control")
                    value = data.get("value")
                    channel = data.get("channel", 0)

                    if type(channel) is not int or not 0 <= channel <= 15:
                        logger.warning("Ignoring invalid MIDI channel: %s", channel)
                        continue

                    if msg_type == "cc":
                        if (
                            type(control) is int
                            and 0 <= control <= 127
                            and type(value) is int
                            and 0 <= value <= 127
                        ):
                            if self._is_studioone():
                                self._s1_control_change(control, value, channel)
                            else:
                                self.midi_handler.send_control_change(control, value, channel=channel)
                    elif msg_type in ("noteon", "note"):
                        velocity = data.get("value", data.get("velocity", 100))
                        if (
                            type(control) is int
                            and 0 <= control <= 127
                            and type(velocity) is int
                            and 0 <= velocity <= 127
                        ):
                            if self._is_studioone():
                                if velocity > 0:
                                    self._s1_send(mido.Message("note_on", channel=channel, note=control, velocity=velocity))
                                else:
                                    self._s1_send(mido.Message("note_off", channel=channel, note=control, velocity=0))
                            else:
                                self.midi_handler.send_note_on(control, velocity, channel=channel)
                    elif msg_type == "noteoff":
                        if type(control) is int and 0 <= control <= 127:
                            if self._is_studioone():
                                self._s1_send(mido.Message("note_off", channel=channel, note=control, velocity=0))
                            else:
                                self.midi_handler.send_note_off(control, channel=channel)
                    elif msg_type == "pitchbend":
                        if type(value) is int and 0 <= value <= 127:
                            if self._is_studioone():
                                bend = round((value - 64) / 64 * 8191) if value > 63 else round((value - 64) * 8192 / 64)
                                bend = max(-8192, min(8191, bend))
                                self._s1_send(mido.Message("pitchwheel", channel=channel, pitch=bend))
                            else:
                                self.midi_handler.send_pitch_bend(value, channel=channel)

                    logger.debug(f"Received: {data}")

                except (json.JSONDecodeError, TypeError, KeyError) as e:
                    logger.error(f"Error parsing message '{message}': {e}")
                    
        except websockets.exceptions.ConnectionClosed:
            logger.info("Client connection closed abruptly")
        finally:
            self._clients.discard(websocket)
            logger.info(f"Client disconnected {websocket.remote_address}")

    async def broadcast(self, payload: dict[str, Any], cache: bool = True) -> None:
        """Sends a server-pushed message to every connected client.

        The payload is cached by type so newly connected clients (e.g.
        the app after a reconnect) receive the latest state too. Transient
        streams (live wheel positions) pass ``cache=False``.
        """
        if cache and isinstance(payload.get("type"), str):
            self._relayed[payload["type"]] = payload
        if not self._clients:
            return
        message = json.dumps(payload)
        stale = []
        for client in list(self._clients):
            try:
                await client.send(message)
            except Exception:
                stale.append(client)
        for client in stale:
            self._clients.discard(client)

    async def start(self) -> None:
        """Starts the WebSocket server and runs until stopped."""
        logger.info(f"Starting WebSocket server on {self.host}:{self.port}")
        async with websockets.serve(self._handle_connection, self.host, self.port):
            await self._shutdown_event.wait()

    def stop(self) -> None:
        """Signals the server to stop."""
        logger.info("Stopping WebSocket server...")
        self._shutdown_event.set()

if __name__ == "__main__":
    from core.midi import MidiInterface
    
    # Simple integration test
    midi = MidiInterface(port_name="Test Bridge")
    server = WebSocketServer(host="localhost", port=8765, midi_handler=midi)
    
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    try:
        loop.run_until_complete(server.start())
    except KeyboardInterrupt:
        server.stop()
    finally:
        loop.close()