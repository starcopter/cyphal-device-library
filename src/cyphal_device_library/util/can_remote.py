"""Register python-can's ``remote`` interface with pycyphal.

pycyphal ships constructors for common python-can backends (``socketcan``,
``usbtingo``, ``pcan`` …) but not for ``remote``. Without this registration a
transport created via :func:`make_can_transport` with a ``remote:`` iface fails
with ``Interface not supported yet``.

The iface channel is ``<host>:<port>`` (e.g. ``remote:127.0.0.1:43113``).
"""

from __future__ import annotations


def register_can_remote_constructor() -> None:
    """Idempotently add ``remote`` support to pycyphal's PythonCAN media."""
    from typing import Any

    import can
    from pycyphal.transport.can.media.pythoncan import _pythoncan as pythoncan_media

    if "remote" in pythoncan_media._CONSTRUCTORS:
        return

    def _construct(
        parameters: pythoncan_media._InterfaceParameters,
    ) -> tuple[pythoncan_media.PythonCANBusOptions, can.ThreadSafeBus]:
        channel = parameters.channel_name
        if "://" not in channel:
            channel = f"ws://{channel}/"
        kwargs: dict[str, Any] = {
            "interface": "remote",
            "channel": channel,
            "fd": isinstance(parameters, pythoncan_media._FDInterfaceParameters),
        }
        return pythoncan_media.PythonCANBusOptions(), can.ThreadSafeBus(**kwargs)

    pythoncan_media._CONSTRUCTORS["remote"] = _construct
