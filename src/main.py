import asyncio
import logging
import signal
from types import FrameType
from typing import Optional

from server.websocket import WebSocketServer
from core.midi import MidiInterface
from core.discovery import BridgeAnnouncer
from core.physical import KeyboardWatcher
from core.scriptlink import ScriptLink
from core.statefile import StateFileWatcher

# Configure logging for the application
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

class MidiBridgeApp:
    """
    Main application class for the PC Bridge.
    Initializes and manages the lifetime of external dependencies like the WebSocket server.
    """

    def __init__(self, host: str = "0.0.0.0", port: int = 8765) -> None:
        """
        Initializes the application instance.

        :param host: The host address to bind the WebSocket server to.
        :param port: The port for the WebSocket server.
        """
        self.host: str = host
        self.port: int = port
        self.server: Optional[WebSocketServer] = None
        self.script_link: Optional[ScriptLink] = None
        self.state_watcher: Optional[StateFileWatcher] = None
        self.keyboard: Optional[KeyboardWatcher] = None
        self._running: bool = False

    def handle_exit(self, signum: int, frame: Optional[FrameType]) -> None:
        """
        Handles OS signals like SIGTERM and SIGINT for graceful shutdown.
        """
        logger.info(f"Received signal {signum}. Shutting down...")
        self._running = False
        if self.server:
            self.server.stop()

    async def run(self) -> None:
        """
        Starts the WebSocket server and keeps the application running.
        """
        # Initialize dependencies
        midi_handler = MidiInterface()
        if not midi_handler.open():
            logger.warning(
                "MIDI output is unavailable: %s",
                midi_handler.availability_error,
            )
        
        self.server = WebSocketServer(host=self.host, port=self.port, midi_handler=midi_handler)

        # The AbletonBridgeMixer remote script connects over a loopback
        # TCP link and pushes live set data (track names); relay it on.
        async def _relay_script_message(data: dict) -> None:
            if self.server is not None:
                await self.server.broadcast(data)

        self.script_link = ScriptLink(on_message=_relay_script_message)
        await self.script_link.start()

        # Live's embedded Python cannot always open a socket, so the remote
        # script also writes snapshots to a file. Relay those too.
        self.state_watcher = StateFileWatcher(on_message=_relay_script_message)
        await self.state_watcher.start()
        
        # Attach signal handlers for graceful shutdown (UNIX and Windows compatible)
        loop = asyncio.get_running_loop()

        # Mirror the performer's real pitch/mod wheels: the keyboard streams
        # them, the app displays them.
        def _push_wheels(channel: int, pitch, mod) -> None:
            payload = {"type": "wheels", "channel": channel}
            if pitch is not None:
                payload["pitch"] = pitch
            if mod is not None:
                payload["mod"] = mod
            if self.server is not None:
                asyncio.run_coroutine_threadsafe(
                    self.server.broadcast(payload, cache=False), loop
                )

        def _push_device(name) -> None:
            if self.server is not None:
                asyncio.run_coroutine_threadsafe(
                    self.server.broadcast({"type": "input", "name": name}), loop
                )

        self.keyboard = KeyboardWatcher(on_change=_push_wheels, on_device=_push_device)
        self.keyboard.start()

        try:
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(
                    sig, lambda signal_number=sig: self.handle_exit(signal_number, None)
                )
        except NotImplementedError:
            # Signal handlers are only partly supported on Windows with Python < 3.8 or specific event loops
            logger.warning("Signal handlers not fully supported on this platform.")

        self._running = True
        logger.info(f"Starting MIDI Bridge on {self.host}:{self.port}")

        # Advertise ourselves over mDNS so controllers find the bridge at
        # "midi-bridge.local" without any address configuration.
        announcer = BridgeAnnouncer(port=self.port)
        await announcer.start()
        
        try:
            await self.server.start()
                
        except asyncio.CancelledError:
            logger.info("Main run loop cancelled.")
        except Exception as e:
            logger.error(f"Critical error in main loop: {e}")
        finally:
            logger.info("Stopping WebSocket server...")
            await announcer.stop()
            if self.keyboard is not None:
                self.keyboard.stop()
            if self.state_watcher is not None:
                await self.state_watcher.stop()
            if self.script_link is not None:
                await self.script_link.stop()
            if self.server:
                self.server.stop()
            
            midi_handler.close()
            logger.info("Application shutdown complete.")

if __name__ == "__main__":
    app = MidiBridgeApp()
    try:
        asyncio.run(app.run())
    except KeyboardInterrupt:
        # Catch just in case signal handling didn't catch it
        pass