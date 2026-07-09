"""Register python-can's ``udp_multicast`` interface with pycyphal.

pycyphal ships constructors for common python-can backends (``socketcan``,
``usbtingo``, ``pcan`` …) but not for ``udp_multicast``. Without this
registration a transport created via :func:`make_can_transport` with an
``udp_multicast:`` iface fails with ``Interface not supported yet``.

The iface channel may optionally embed the UDP port as ``<multicast-ip>:<port>``
(e.g. ``udp_multicast:239.74.163.3:43113``); when the port is omitted python-can's
default multicast port is used.
"""

from __future__ import annotations


def register_udp_multicast_constructor() -> None:
    """Idempotently add ``udp_multicast`` support to pycyphal's PythonCAN media."""
    from typing import Any

    import can
    from pycyphal.transport.can.media.pythoncan import _pythoncan as pythoncan_media

    if "udp_multicast" in pythoncan_media._CONSTRUCTORS:
        return

    def _construct(
        parameters: pythoncan_media._InterfaceParameters,
    ) -> tuple[pythoncan_media.PythonCANBusOptions, can.ThreadSafeBus]:
        multicast_ip, _, port_text = parameters.channel_name.partition(":")
        kwargs: dict[str, Any] = {
            "interface": "udp_multicast",
            "channel": multicast_ip,
            "fd": isinstance(parameters, pythoncan_media._FDInterfaceParameters),
        }
        if port_text:
            kwargs["port"] = int(port_text)
        return pythoncan_media.PythonCANBusOptions(), can.ThreadSafeBus(**kwargs)

    pythoncan_media._CONSTRUCTORS["udp_multicast"] = _construct
