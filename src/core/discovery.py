"""Advertises the bridge over mDNS so apps can find it as ``midi-bridge.local``.

The mobile app tries this name automatically, which makes Wi-Fi setup
effectively zero-configuration: no typing the PC's IP address, and it keeps
working even when DHCP hands the PC a new address.

Uses zeroconf's asyncio API because the bridge runs inside an event loop.
"""

import asyncio
import logging
import socket

logger = logging.getLogger(__name__)

SERVICE_TYPE = "_midi-bridge._tcp.local."
DEFAULT_SERVICE_NAME = "midi-bridge"


def _local_ip_address() -> str:
    """Best-effort detection of the LAN address of this machine."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("8.8.8.8", 80))
        return probe.getsockname()[0]
    finally:
        probe.close()


class BridgeAnnouncer:
    """Registers the bridge as a mDNS service while the process is alive."""

    def __init__(self, port: int, name: str = DEFAULT_SERVICE_NAME, properties=None) -> None:
        self.port = port
        self.name = name
        self.properties = properties or {}
        self._zeroconf = None
        self._info = None
        self._address = None
        self._refresh_task = None

    async def start(self) -> bool:
        """Publishes the service. Returns True when the announcement succeeded."""
        try:
            from zeroconf.asyncio import AsyncServiceInfo, AsyncZeroconf
        except ImportError:
            logger.warning(
                "zeroconf is not installed - mDNS discovery disabled "
                "(pip install zeroconf). Apps fall back to the last saved address."
            )
            return False

        try:
            address = _local_ip_address()
            if not address or address.startswith("127."):
                raise OSError("no LAN IPv4 address found")
            logger.info("Announcing mDNS name on %s:%s...", address, self.port)
            self._zeroconf = AsyncZeroconf()
            self._address = address
            self._info = AsyncServiceInfo(
                SERVICE_TYPE,
                f"{self.name}.{SERVICE_TYPE}",
                addresses=[socket.inet_aton(address)],
                port=self.port,
                properties=self.properties,
            )
            await self._zeroconf.async_register_service(self._info, allow_name_change=True)
            logger.info(
                "Announced on the network as %s.local (port %s, address %s).",
                self.name,
                self.port,
                address,
            )
            self._refresh_task = asyncio.create_task(self._refresh_loop())
            return True
        except Exception as error:
            logger.warning(
                "Could not announce the mDNS name (%s: %s). Auto-discovery will fall "
                "back to the last saved address.",
                type(error).__name__,
                error,
            )
            await self.stop()
            return False

    async def _refresh_loop(self) -> None:
        """Keeps the announcement truthful: re-publishes whenever the PC's
        LAN address changes (Wi-Fi switch, DHCP renewal) so that
        midi-bridge.local always resolves to the machine's live IP."""
        while True:
            await asyncio.sleep(60)
            try:
                if self._zeroconf is None or self._info is None:
                    return
                address = _local_ip_address()
                if address and not address.startswith("127.") and address != self._address:
                    logger.info(
                        "LAN address changed (%s -> %s) - updating mDNS announcement.",
                        self._address,
                        address,
                    )
                    self._address = address
                    self._info.addresses = [socket.inet_aton(address)]
                    await self._zeroconf.async_update_service(self._info)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.warning("mDNS refresh failed (%s: %s)", type(error).__name__, error)

    async def stop(self) -> None:
        """Unregisters the service and shuts the announcer down."""
        if self._refresh_task is not None:
            self._refresh_task.cancel()
            try:
                await self._refresh_task
            except asyncio.CancelledError:
                pass
            self._refresh_task = None
        if self._zeroconf is None:
            return
        try:
            if self._info is not None:
                await self._zeroconf.async_unregister_service(self._info)
            await self._zeroconf.async_close()
        except Exception:
            logger.debug("Failed to shut down zeroconf cleanly.", exc_info=True)
        self._zeroconf = None
        self._info = None
