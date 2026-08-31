"""ENOBUFS lives on the exception, not in pycyphal's 'publisher task exception' message."""

import logging

from cyphal_device_library.util._logging import Errno105Filter


def _record(*, msg: str, exc: BaseException | None = None) -> logging.LogRecord:
    exc_info = (type(exc), exc, None) if exc is not None else None
    return logging.LogRecord(
        name="pycyphal.application.heartbeat_publisher",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg=msg,
        args=(),
        exc_info=exc_info,
    )


def test_errno105_filter_allows_unrelated_errors() -> None:
    record = _record(msg="publisher task exception", exc=RuntimeError("boom"))
    assert Errno105Filter().filter(record) is True


def test_errno105_filter_drops_message_text() -> None:
    record = _record(msg="OSError: [Errno 105] No buffer space available")
    assert Errno105Filter().filter(record) is False


def test_errno105_filter_drops_exc_info_enobufs() -> None:
    record = _record(
        msg="HeartbeatPublisher(...) publisher task exception",
        exc=OSError(105, "No buffer space available"),
    )
    assert Errno105Filter().filter(record) is False
