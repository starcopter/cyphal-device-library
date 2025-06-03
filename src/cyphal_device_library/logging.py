from __future__ import annotations

import logging
from copy import copy
from datetime import datetime
from typing import TYPE_CHECKING, Any, Dict, Optional

import pycyphal.transport
import uavcan.diagnostic
import uavcan.node

if TYPE_CHECKING:
    from logging import _FormatStyle


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
        meta = {
            "node_id": transfer.source_node_id or -1,
            "transfer_timestamp": datetime.fromtimestamp(float(transfer.timestamp.system)).isoformat(
                timespec="microseconds"
            ),
            "timestamp": record.timestamp.microsecond * 1e-6,
            "severity": self.UAVCAN_SEVERITY_NAMES[record.severity.value],
        }

        if heartbeat is not None:
            meta |= {
                "node_health": self.NODE_HEALTH_NAMES[heartbeat.health.value],
                "node_mode": self.NODE_MODE_NAMES[heartbeat.mode.value],
                "node_vssc": heartbeat.vendor_specific_status_code,
            }
        else:
            meta |= {"node_health": None, "node_mode": None, "node_vssc": 0}

        if info is not None:
            meta |= {
                "app_name": info.name.tobytes().decode("utf8", errors="replace"),
                "node_uid": info.unique_id.tobytes().hex(),
            }
        else:
            meta |= {"app_name": None, "node_uid": None}

        return meta

    def formatMessage(self, log_record: logging.LogRecord) -> str:
        recordcopy = copy(log_record)
        recordcopy.__dict__ |= self.metadata(**recordcopy.__dict__)
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
