import asyncio
import logging
import os
import shutil
import signal
import subprocess
import sys
from types import FrameType
from typing import Optional

from server.websocket import WebSocketServer
from core.daw import DawManager
from core.midi import MidiInterface
from core.discovery import BridgeAnnouncer
from core.physical import KeyboardWatcher
from core.scriptlink import ScriptLink
from core.statefile import StateFileWatcher
from core.studioone import StudioOneMirror, INPUT_PORT_NAME as STUDIOONE_PORT

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
        self.daw_manager: DawManager = DawManager()
        self.script_link: Optional[ScriptLink] = None
        self.state_watcher: Optional[StateFileWatcher] = None
        self.s1_mirror: Optional[StudioOneMirror] = None
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

    def _on_daw_changed(self, daw: str) -> None:
        """Applies a DAW switch without disturbing the Windows device list.

        Studio One crashes when MIDI devices appear or disappear while it is
        running, and Windows invalidates winmm handles whenever the device list
        changes. So a switch between the two MIDI backends (hosted virtual
        ports for Ableton, the existing loopMIDI port for Studio One) restarts
        the bridge instead of reconfiguring it in place. The persisted DAW
        choice makes the new process start in the right mode.
        """
        if self.server is None:
            return
        hosting = self.server.midi_handler.allow_virtual_hosting
        needs_new_backend = (daw == "studioone" and hosting) or (
            daw != "studioone" and not hosting
        )
        if needs_new_backend:
            self._restart_bridge()
            return
        # Same backend: only the mirror needs to start or stop.
        if daw == "studioone":
            if self.s1_mirror is not None:
                self.s1_mirror.start(asyncio.get_running_loop())
        elif self.s1_mirror is not None:
            self.s1_mirror.stop()

    def _restart_bridge(self) -> None:
        """Relaunches the bridge so it picks up the new DAW MIDI backend."""
        executable = sys.executable
        if not getattr(sys, "frozen", False):
            # A source run reports the protected WindowsApps path; the
            # user-facing python.exe alias is the one that can be launched.
            executable = shutil.which("python") or executable
        command = " ".join('"%s"' % part for part in [executable] + sys.argv[1:])
        logger.info("Restarting the bridge to apply the new DAW MIDI backend: %s", command)
        try:
            subprocess.Popen(
                "ping -n 3 127.0.0.1 >nul & " + command,
                shell=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception:
            logger.exception("Could not relaunch the bridge; the watchdog will")
        # Let the delayed relaunch own the ports from here on.
        os._exit(0)

    async def run(self) -> None:
        """
        Starts the WebSocket server and keeps the application running.
        """
        # Initialize dependencies. Studio One's surface echo arrives on the
        # loopMIDI port; Windows will not let a process open that input while it
        # also hosts teVirtualMIDI ports, so Studio One mode reuses the existing
        # loopMIDI output instead of creating AbletonBridge in-process.
        studio_one = self.daw_manager.daw == "studioone"
        midi_handler = MidiInterface(
            port_name=STUDIOONE_PORT if studio_one else None,
            allow_virtual_hosting=not studio_one,
        )
        if not midi_handler.open():
            logger.warning(
                "MIDI output is unavailable: %s",
                midi_handler.availability_error,
            )
        
        self.server = WebSocketServer(
            host=self.host,
            port=self.port,
            midi_handler=midi_handler,
            daw_manager=self.daw_manager,
            on_daw_changed=self._on_daw_changed,
        )

        # The AbletonBridgeMixer remote script connects over a loopback
        # TCP link and pushes live set data (track names); relay it on.
        async def _relay_script_message(data: dict) -> None:
            # The script's keepalive only exists so it can detect a dead
            # loopback link; it carries no set data, so never show it to apps.
            if data.get("type") == "heartbeat":
                return
            # Only the selected DAW's state reaches the app. A payload
            # without a "daw" field comes from the original Ableton script.
            if not self.daw_manager.matches(data):
                return
            if self.server is not None:
                await self.server.broadcast(data)

        # The Ableton remote script targets port 8767 by design; the
        # env var lets a dev run sidestep the production bridge's port.
        self.script_link = ScriptLink(
            on_message=_relay_script_message,
            port=int(os.environ.get("SCRIPTLINK_PORT", "8767")),
        )
        await self.script_link.start()

        # Live's embedded Python cannot always open a socket, so the remote
        # script also writes snapshots to a file. Relay those too.
        self.state_watcher = StateFileWatcher(on_message=_relay_script_message)
        await self.state_watcher.start()

        # Studio One has no embedded script host, so it mirrors its
        # mixer as MIDI echoes from the Jarvis Bridge surface; decode
        # them into the same mixer_state frames.
        self.s1_mirror = StudioOneMirror(on_message=_relay_script_message)
        self.s1_mirror.start(asyncio.get_running_loop())
        # App-tapped insert/send: let the mirror focus that device on the
        # surface so Studio One streams its parameter details back.
        if self.server is not None:
            self.server.on_device_request = self.s1_mirror.request_device
        
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
            if self.s1_mirror is not None:
                self.s1_mirror.stop()
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