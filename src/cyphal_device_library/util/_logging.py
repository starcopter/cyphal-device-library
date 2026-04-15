from __future__ import annotations

import logging
from copy import copy
from datetime import datetime
from enum import IntEnum
from typing import TYPE_CHECKING, Any, Dict, Optional

import pycyphal.transport
import uavcan.diagnostic
import uavcan.node

if TYPE_CHECKING:
    from logging import _FormatStyle


class UAVCANDiagnosticSeverity(IntEnum):
    TRACE = 0
    DEBUG = 1
    INFO = 2
    NOTICE = 3
    WARNING = 4
    ERROR = 5
    CRITICAL = 6
    ALERT = 7


UAVCAN_SEVERITY_TO_PYTHON = {
    UAVCANDiagnosticSeverity.TRACE: 5,  # DEBUG - 5
    UAVCANDiagnosticSeverity.DEBUG: logging.DEBUG,
    UAVCANDiagnosticSeverity.INFO: logging.INFO,
    UAVCANDiagnosticSeverity.NOTICE: 25,  # INFO + 25
    UAVCANDiagnosticSeverity.WARNING: logging.WARNING,
    UAVCANDiagnosticSeverity.ERROR: logging.ERROR,
    UAVCANDiagnosticSeverity.CRITICAL: logging.CRITICAL,
    UAVCANDiagnosticSeverity.ALERT: 60,  # CRITICAL + 10
}


def patch_log_levels_in_python_logging_module() -> None:
    """Patch the Python logging module to add log levels for UAVCAN severity levels."""
    TRACE = UAVCAN_SEVERITY_TO_PYTHON[UAVCANDiagnosticSeverity.TRACE]
    NOTICE = UAVCAN_SEVERITY_TO_PYTHON[UAVCANDiagnosticSeverity.NOTICE]
    ALERT = UAVCAN_SEVERITY_TO_PYTHON[UAVCANDiagnosticSeverity.ALERT]

    assert logging.NOTSET < TRACE < logging.DEBUG, "TRACE level must be between NOTSET and DEBUG"
    assert logging.INFO < NOTICE < logging.WARNING, "NOTICE level must be between INFO and WARNING"
    assert logging.CRITICAL < ALERT, "ALERT level must be greater than CRITICAL"

    logging.addLevelName(TRACE, "TRACE")
    logging.addLevelName(NOTICE, "NOTICE")
    logging.addLevelName(ALERT, "ALERT")


class Errno105Filter(logging.Filter):
    """Filter out log messages that contain '[Errno 105] No buffer space available.'

    This can be used to suppress pycyphal errors that would otherwise spam the logs.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        return "[Errno 105] No buffer space available" not in record.getMessage()

    @staticmethod
    def apply_to(logger_or_handler: str | logging.Logger | logging.Handler) -> None:
        if not isinstance(logger_or_handler, (logging.Logger, logging.Handler)):
            logger_or_handler = logging.getLogger(logger_or_handler)
        logger_or_handler.addFilter(Errno105Filter())


LOGFMT_ESCAPE = str.maketrans({r'"': r"\"", "\\": r"\\"})


class DiagnosticRecordFormatter(logging.Formatter):
    UAVCAN_SEVERITY_NAMES = {
        uavcan.diagnostic.Severity_1.TRACE: "TRACE",
        uavcan.diagnostic.Severity_1.DEBUG: "DEBUG",
        uavcan.diagnostic.Severity_1.INFO: "INFO",
        uavcan.diagnostic.Severity_1.NOTICE: "NOTICE",
        uavcan.diagnostic.Severity_1.WARNING: "WARNING",
        uavcan.diagnostic.Severity_1.ERROR: "ERROR",
        uavcan.diagnostic.Severity_1.CRITICAL: "CRITICAL",
        uavcan.diagnostic.Severity_1.ALERT: "ALERT",
    }

    NODE_HEALTH_NAMES = {
        uavcan.node.Health_1.NOMINAL: "NOMINAL",
        uavcan.node.Health_1.ADVISORY: "ADVISORY",
        uavcan.node.Health_1.CAUTION: "CAUTION",
        uavcan.node.Health_1.WARNING: "WARNING",
    }

    NODE_MODE_NAMES = {
        uavcan.node.Mode_1.OPERATIONAL: "OPERATIONAL",
        uavcan.node.Mode_1.INITIALIZATION: "INITIALIZATION",
        uavcan.node.Mode_1.MAINTENANCE: "MAINTENANCE",
        uavcan.node.Mode_1.SOFTWARE_UPDATE: "SOFTWARE_UPDATE",
    }

    def metadata(
        self,
        record: uavcan.diagnostic.Record_1,
        transfer: pycyphal.transport.TransferFrom,
        heartbeat: Optional[uavcan.node.Heartbeat_1],
        info: Optional[uavcan.node.GetInfo_1.Response],
        **kwargs,
    ) -> Dict[str, Any]:
        meta: Dict[str, Any] = {
            "node_id": transfer.source_node_id or -1,
            "transfer_timestamp": datetime.fromtimestamp(float(transfer.timestamp.system)).isoformat(
                timespec="microseconds"
            ),
            "timestamp": record.timestamp.microsecond * 1e-6,
            "severity": self.UAVCAN_SEVERITY_NAMES[record.severity.value],
        }

        if heartbeat is not None:
            meta.update(
                {
                    "node_health": self.NODE_HEALTH_NAMES[heartbeat.health.value],
                    "node_mode": self.NODE_MODE_NAMES[heartbeat.mode.value],
                    "node_vssc": heartbeat.vendor_specific_status_code,
                }
            )
        else:
            meta.update({"node_health": None, "node_mode": None, "node_vssc": 0})

        if info is not None:
            meta.update(
                {
                    "app_name": info.name.tobytes().decode("utf8", errors="replace"),
                    "node_uid": info.unique_id.tobytes().hex(),
                }
            )
        else:
            meta.update({"app_name": None, "node_uid": None})

        return meta

    def formatMessage(self, record: logging.LogRecord) -> str:
        recordcopy = copy(record)
        recordcopy.__dict__.update(self.metadata(**recordcopy.__dict__))
        recordcopy.message = recordcopy.message.translate(LOGFMT_ESCAPE)
        return super().formatMessage(recordcopy)

    def __init__(
        self,
        fmt: str | None = None,
        datefmt: str | None = None,
        style: "_FormatStyle" = "%",
        validate: bool = True,
    ) -> None:
        if not fmt:
            fmt = (
                "ts_local=%(transfer_timestamp)s ts_sync=%(timestamp).6f id=%(node_id)d "
                'severity=%(severity)s msg="%(message)s" uid=%(node_uid)s name=%(app_name)s '
                "health=%(node_health)s mode=%(node_mode)s vssc=%(node_vssc)d"
            )
            style = "%"
        super().__init__(fmt, datefmt, style, validate)
