import logging
from enum import IntEnum


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
