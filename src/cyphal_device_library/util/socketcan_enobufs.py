"""Work around pycyphal SocketCANMedia bugs that show up on real hardware.

ENOBUFS
    Physical CAN requires another controller to ACK each frame. On an empty bus
    the kernel TX queue fills and ``sock.send`` raises
    ``OSError: [Errno 105] No buffer space available``. pycyphal already maps
    ``asyncio.TimeoutError`` to a failed send (return 0); we apply the same
    policy to ENOBUFS.

Unreachable assertion in ``_read_frame``
    After pycyphal #375, ``_parse_native_frame`` still returns ``None`` for RTR
    frames and for CAN error frames it does not map (lost arbitration, ACK
    error, ``CAN_ERR_CRTL_ACTIVE``, …). ``_read_frame`` then hits
    ``assert False, "Unreachable"``, the receive thread logs ERROR, sleeps 1s,
    and continues. The monitor keeps working, but RX stalls for a second.
    The pre-#375 behavior was to skip those frames; restore that.
"""

from __future__ import annotations

import errno
import logging
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

logger = logging.getLogger(__name__)

_ENOBUFS = getattr(errno, "ENOBUFS", 105)
_UNREACHABLE = "Unreachable"
_installed = False

SendMethod = Callable[..., Awaitable[int]]
T = TypeVar("T")
ReadFrameMethod = Callable[..., T]


def wrap_send_tolerating_enobufs(send: SendMethod) -> SendMethod:
    """Return ``send`` wrapped so ``OSError(ENOBUFS)`` yields ``0`` (timeout)."""

    async def wrapped(*args: Any, **kwargs: Any) -> int:
        try:
            return await send(*args, **kwargs)
        except OSError as err:
            if err.errno == _ENOBUFS:
                logger.debug("SocketCAN TX queue full (ENOBUFS); treating as send timeout")
                return 0
            raise

    return wrapped


def wrap_read_frame_skipping_unreachable(read_frame: ReadFrameMethod[T]) -> ReadFrameMethod[T]:
    """Retry ``_read_frame`` when pycyphal asserts on an ignored native frame.

    ``_parse_native_frame`` returning ``None`` is expected (dropped RTR /
    unmapped error frames). pycyphal currently treats that as unreachable.
    """

    def wrapped(self: object, ts_mono_ns: int) -> T:
        while True:
            try:
                return read_frame(self, ts_mono_ns)
            except AssertionError as exc:
                if str(exc) != _UNREACHABLE:
                    raise
                logger.debug("Ignoring SocketCAN frame that pycyphal could not parse")

    return wrapped


def apply_socketcan_media_patches(media_cls: Any) -> None:
    """Patch send always; patch ``_read_frame`` only if that private method exists."""
    media_cls.send = wrap_send_tolerating_enobufs(media_cls.send)
    read_frame = getattr(media_cls, "_read_frame", None)
    if read_frame is None:
        logger.debug("%s has no _read_frame; skipping Unreachable workaround", media_cls)
        return
    media_cls._read_frame = wrap_read_frame_skipping_unreachable(read_frame)


def install_socketcan_enobufs_tolerance() -> None:
    """Idempotently patch SocketCANMedia send/recv for ENOBUFS and ignored frames.

    No-op when SocketCAN media is unavailable (non-Linux platforms).
    """
    global _installed
    if _installed:
        return
    try:
        from pycyphal.transport.can.media.socketcan import SocketCANMedia
    except ImportError:
        _installed = True
        return

    apply_socketcan_media_patches(SocketCANMedia)
    _installed = True
