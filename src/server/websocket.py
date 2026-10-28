import asyncio
import json
import logging
from typing import Any

import websockets

from core import commandfile
from core.midi import MidiInterface

logging.basicConfig(level=logging.INFO)
logger: logging.Logger = logging.getLogger("WSServer")

class WebSocketServer:
    """Manages WebSocket server to receive control data from mobile."""

    def __init__(self, host: str, port: int, midi_handler: MidiInterface) -> None:
        self.host: str = host
        self.port: int = port
        self.midi_handler: MidiInterface = midi_handler
        self._shutdown_event: asyncio.Event = asyncio.Event()
        self._clients: set[Any] = set()
        self._relayed: dict[str, Any] = {}

    def _status_payload(self) -> dict[str, Any]:
        """Builds the status message sent to clients on connect and on demand."""
        return {
            "type": "status",
            "midi_available": self.midi_handler.is_open,
            "midi_output": self.midi_handler.port_name,
            "available_outputs": self.midi_handler.available_outputs,
            "midi_error": self.midi_handler.availability_error,
        }

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

                    if msg_type == "metronome":
                        commandfile.push_metronome(bool(data.get("value")))
                        continue

                    if msg_type == "select_output":
                        selected = data.get("name")
                        if isinstance(selected, str) and selected.strip():
                            self.midi_handler.set_output(selected)
                            await websocket.send(json.dumps(self._status_payload()))
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
                            self.midi_handler.send_control_change(control, value, channel=channel)
                    elif msg_type in ("noteon", "note"):
                        velocity = data.get("value", data.get("velocity", 100))
                        if (
                            type(control) is int
                            and 0 <= control <= 127
                            and type(velocity) is int
                            and 0 <= velocity <= 127
                        ):
                            self.midi_handler.send_note_on(control, velocity, channel=channel)
                    elif msg_type == "noteoff":
                        if type(control) is int and 0 <= control <= 127:
                            self.midi_handler.send_note_off(control, channel=channel)
                    elif msg_type == "pitchbend":
                        if type(value) is int and 0 <= value <= 127:
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