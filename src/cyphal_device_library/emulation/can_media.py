"""Shared CAN media for multi-node emulation on one physical interface.

Why not ``make_can_transport``?
-------------------------------

Helpers such as :func:`cyphal_device_library.util.make_can_transport` (and
``pycyphal.application.make_transport``) build **one** Cyphal node: a single
:class:`~pycyphal.transport.can.CANTransport` bound to **one** node ID on **one**
opened CAN adapter.

Device emulation needs **several** logical Cyphal nodes (for example multiple
emulated BMS packs at node IDs 61, 62, …) on the **same** USB/CAN interface,
often while an application client is already using that bus.

Calling ``make_can_transport`` once per emulated node is not viable:

- Each call opens a new low-level :class:`~pycyphal.transport.can.media.Media`
  on the same interface, which commonly fails on real hardware with
  ``LIBUSB_ERROR_BUSY``.
- Even on virtual buses, separate media instances do not share RX traffic the
  way multiple node IDs on one adapter should.

pycyphal's CAN stack also assumes one media instance per transport:

- :class:`~pycyphal.transport.can.CANTransport` calls ``media.start(handler)``
  in its constructor; drivers accept only **one** RX handler.
- :meth:`~pycyphal.transport.can.CANTransport.close` calls ``media.close()``,
  which would tear down the adapter for every node if they shared raw media.

This module provides the glue pycyphal does not ship:

:class:`SharedCANMedia`
    Multiplexes one underlying ``Media`` across several ``CANTransport``
    instances. ``close()`` is a no-op so stopping one emulated node does not
    release the bus; call :meth:`SharedCANMedia.shutdown` when the session ends.

:func:`create_can_media`
    Opens only the low-level ``Media`` (SocketCAN, PythonCAN, …) with the
    correct bitrate/MTU, **without** binding a node ID. Used when emulation
    starts without an existing client transport (for example websocket
    ``emulate.start``).

:func:`extract_can_media`
    Returns the ``Media`` already owned by a running transport so emulated nodes
    can attach to the same adapter (for example BMS set emulation reusing the
    CLI client's ``get_can3_transport`` stack).

Typical layout::

    # Application client (one node ID):
    client_transport = make_can_transport(iface, bitrate, node_id=124)

    # Emulated packs (other node IDs, same adapter):
    shared = SharedCANMedia(extract_can_media(client_transport), already_running=True)
    pack_a = CANTransport(shared, 61)
    pack_b = CANTransport(shared, 62)
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

import pycyphal.transport
from pycyphal.transport import Timestamp
from pycyphal.transport.can import CANTransport
from pycyphal.transport.can.media import Media
from pycyphal.transport.can.media._filter import FilterConfiguration
from pycyphal.transport.can.media._frame import Envelope
from pycyphal.transport.redundant import RedundantTransport


def create_can_media(
    interface: str,
    *,
    bitrate: int | list[int] | None = None,
    mtu: int | None = None,
) -> Media:
    """Open one CAN media instance for an interface string.

    Mirrors the media construction rules from ``pycyphal.application.make_transport``
    without creating a :class:`~pycyphal.transport.can.CANTransport` or node ID.
    """
    if bitrate is None:
        br_arb, br_data = 1_000_000, 5_000_000
        resolved_mtu = mtu if mtu is not None else 64
    elif isinstance(bitrate, int):
        br_arb = br_data = bitrate
        resolved_mtu = mtu if mtu is not None else 8
    else:
        br_arb, br_data = bitrate
        if len(bitrate) != 2:
            raise ValueError("Only 2 bitrates are supported")
        resolved_mtu = mtu if mtu is not None else (8 if br_arb == br_data else 64)

    iface = str(interface).strip()
    if not iface:
        raise ValueError("CAN interface is required")

    if iface.lower().startswith("socketcan:"):
        from pycyphal.transport.can.media.socketcan import SocketCANMedia

        return SocketCANMedia(iface.split(":", 1)[-1], mtu=resolved_mtu, disable_brs=br_arb == br_data)
    if iface.lower().startswith("candump:"):
        from pycyphal.transport.can.media.candump import CandumpMedia

        return CandumpMedia(iface.split(":", 1)[-1])
    if iface.lower().startswith("socketcand:"):
        from pycyphal.transport.can.media.socketcand import SocketcandMedia

        params = iface.split(":")
        channel = params[1]
        host = params[2]
        port = int(params[3]) if len(params) == 4 else 29536
        return SocketcandMedia(channel, host, port)

    from pycyphal.transport.can.media.pythoncan import PythonCANMedia

    pythoncan_bitrate = br_arb if br_arb == br_data else (br_arb, br_data)
    return PythonCANMedia(iface, pythoncan_bitrate, resolved_mtu)


def extract_can_media(transport: pycyphal.transport.Transport | None) -> Media | None:
    """Return the first CAN media instance attached to a transport, if any."""
    if transport is None:
        return None
    if isinstance(transport, CANTransport):
        return transport._maybe_media  # noqa: SLF001
    if isinstance(transport, RedundantTransport):
        for inferior in transport.inferiors:
            media = extract_can_media(inferior)
            if media is not None:
                return media
    return None


class SharedCANMedia(Media):
    """Start underlying CAN media once and forward RX to every registered handler.

    See the :mod:`can_media` module docstring for why this wrapper exists instead
    of calling :func:`cyphal_device_library.util.make_can_transport` per emulated
    node.
    """

    def __init__(self, media: Media, *, owns_media: bool = True, already_running: bool = False) -> None:
        """Initialize the shared CAN media."""
        self._media = media
        self._owns_media = owns_media
        self._started = already_running
        self._closed = False
        self._handlers: list[Media.ReceivedFramesHandler] = []
        self._error_handlers: list[Media.ErrorHandler | None] = []
        self._upstream_handler: Media.ReceivedFramesHandler | None = None
        self._sending_via_shared = False
        if already_running:
            self._chain_existing_rx_handler()
        self._wrap_underlying_send()

    def _chain_existing_rx_handler(self) -> None:
        """Fan in frames from media that was already started by another transport."""
        existing = getattr(self._media, "_rx_handler", None)
        if not callable(existing):
            return
        self._upstream_handler = existing

        def chained(frames: Sequence[tuple[Timestamp, Envelope]]) -> None:
            existing(frames)
            self._dispatch_frames(frames)

        setattr(self._media, "_rx_handler", chained)

    def _wrap_underlying_send(self) -> None:
        """Fan out transmitted frames locally when the bus does not loop back (e.g. virtual:)."""
        original_send = self._media.send

        async def wrapped_send(frames: Iterable[Envelope], monotonic_deadline: float) -> int:
            frame_list = list(frames)
            count = await original_send(frame_list, monotonic_deadline)
            if count > 0:
                stamped = [(Timestamp.now(), envelope) for envelope in frame_list[:count]]
                self._dispatch_frames(stamped)
                if self._sending_via_shared and self._upstream_handler is not None:
                    self._upstream_handler(stamped)
            return count

        setattr(self._media, "send", wrapped_send)

    @property
    def mtu(self) -> int:
        """Return the MTU for the shared CAN media."""
        return self._media.mtu

    @property
    def interface_name(self) -> str:
        """Return the interface name for the shared CAN media."""
        return self._media.interface_name

    @property
    def number_of_acceptance_filters(self) -> int:
        """Return the number of acceptance filters for the shared CAN media."""
        return self._media.number_of_acceptance_filters

    def configure_acceptance_filters(self, configuration: Sequence[FilterConfiguration]) -> None:
        """Configure the acceptance filters for the shared CAN media."""
        self._media.configure_acceptance_filters(configuration)

    def _dispatch_frames(self, frames: Sequence[tuple[Timestamp, Envelope]]) -> None:
        for handler in list(self._handlers):
            handler(frames)

    def _dispatch_error(self, timestamp: Timestamp, error: Media.Error) -> None:
        for handler in self._error_handlers:
            if handler is not None:
                handler(timestamp, error)

    def start(
        self,
        handler: Media.ReceivedFramesHandler,
        no_automatic_retransmission: bool,
        error_handler: Media.ErrorHandler | None = None,
    ) -> None:
        """Start the shared CAN media handler."""
        self._handlers.append(handler)
        self._error_handlers.append(error_handler)
        if self._started:
            return
        self._started = True
        self._media.start(
            self._dispatch_frames,
            no_automatic_retransmission=no_automatic_retransmission,
            error_handler=self._dispatch_error,
        )

    async def send(self, frames: Iterable[Envelope], monotonic_deadline: float) -> int:
        """Send frames to the underlying media."""
        self._sending_via_shared = True
        try:
            return await self._media.send(frames, monotonic_deadline)
        finally:
            self._sending_via_shared = False

    def close(self) -> None:
        """No-op so individual emulated node transports do not release the bus."""

    def shutdown(self) -> None:
        """Close the underlying media when this runtime owns it."""
        if self._owns_media and not self._closed:
            self._media.close()
            self._closed = True
            self._started = False
            self._handlers.clear()
            self._error_handlers.clear()
