"""SocketCAN ENOBUFS is a full TX queue (often an empty bus with no ACK), not a fatal error."""

import errno
from unittest.mock import AsyncMock, MagicMock

import pytest

from cyphal_device_library.util.socketcan_enobufs import (
    apply_socketcan_media_patches,
    wrap_read_frame_skipping_unreachable,
    wrap_send_tolerating_enobufs,
)


@pytest.mark.asyncio
async def test_wrap_send_returns_zero_on_enobufs() -> None:
    send = AsyncMock(side_effect=OSError(errno.ENOBUFS, "No buffer space available"))

    wrapped = wrap_send_tolerating_enobufs(send)
    sent = await wrapped(object(), frames=[], monotonic_deadline=0.0)

    assert sent == 0
    send.assert_awaited_once()


@pytest.mark.asyncio
async def test_wrap_send_reraises_other_oserrors() -> None:
    send = AsyncMock(side_effect=OSError(errno.ENODEV, "No such device"))

    wrapped = wrap_send_tolerating_enobufs(send)
    with pytest.raises(OSError) as exc_info:
        await wrapped(object(), frames=[], monotonic_deadline=0.0)

    assert exc_info.value.errno == errno.ENODEV


@pytest.mark.asyncio
async def test_wrap_send_passes_through_success() -> None:
    send = AsyncMock(return_value=3)

    wrapped = wrap_send_tolerating_enobufs(send)
    sent = await wrapped(object(), frames=["a"], monotonic_deadline=1.5)

    assert sent == 3
    send.assert_awaited_once()


def test_read_frame_retries_after_unreachable_assertion() -> None:
    calls = {"n": 0}

    def original(_self: object, ts_mono_ns: int) -> tuple[str, int]:
        calls["n"] += 1
        if calls["n"] == 1:
            raise AssertionError("Unreachable")
        return ("frame", ts_mono_ns)

    wrapped = wrap_read_frame_skipping_unreachable(original)
    assert wrapped(object(), 123) == ("frame", 123)
    assert calls["n"] == 2


def test_read_frame_reraises_other_assertions() -> None:
    def original(_self: object, _ts_mono_ns: int) -> None:
        raise AssertionError("Unexpected ancillary data: 1, 2, b'x'")

    wrapped = wrap_read_frame_skipping_unreachable(original)
    with pytest.raises(AssertionError, match="ancillary"):
        wrapped(object(), 0)


def test_read_frame_propagates_eagain() -> None:
    def original(_self: object, _ts_mono_ns: int) -> None:
        raise OSError(errno.EAGAIN, "Resource temporarily unavailable")

    wrapped = wrap_read_frame_skipping_unreachable(original)
    with pytest.raises(OSError) as exc_info:
        wrapped(MagicMock(), 0)
    assert exc_info.value.errno == errno.EAGAIN


def test_apply_patches_skips_missing_read_frame() -> None:
    class Media:
        async def send(self, frames: object, monotonic_deadline: float) -> int:
            return 1

    apply_socketcan_media_patches(Media)
    assert not hasattr(Media, "_read_frame")


def test_apply_patches_wraps_read_frame_when_present() -> None:
    class Media:
        async def send(self, frames: object, monotonic_deadline: float) -> int:
            return 1

        def _read_frame(self, ts_mono_ns: int) -> str:
            return f"raw-{ts_mono_ns}"

    apply_socketcan_media_patches(Media)
    assert Media._read_frame(Media(), 7) == "raw-7"
