import logging
import os
import re
from enum import IntEnum

from rich.console import Console
from rich.logging import RichHandler
from rich.padding import Padding

_logger = logging.getLogger(__name__)


def configure_logging(verbosity: int, console: Console | None = None) -> None:
    log_level = {
        0: logging.WARNING,
        1: logging.INFO,
        2: logging.DEBUG,
    }.get(verbosity, logging.NOTSET)

    logging.basicConfig(level=log_level, format="%(message)s", datefmt="[%X]", handlers=[RichHandler(console=console)])
    logging.getLogger("asyncio").setLevel(logging.CRITICAL)


def parse_int_set(text: str) -> set[int]:
    """
    Unpacks the integer set notation.

    Adapted from yakut.int_set_parser.parse_int_set.

    Accepts JSON-list (subset of YAML) of integers at input, too.
    A single scalar is returned as-is unless there is a separator at the end ("125,") or JSON list is used.
    Raises :class:`ValueError` on syntax error.
    Usage:

    >>> parse_int_set("")
    set()
    >>> parse_int_set("123"), parse_int_set("[123]"), parse_int_set("123,")
    ({123}, {123}, {123})
    >>> parse_int_set("-0"), parse_int_set("[-0]"), parse_int_set("-0,")
    ({0}, {0}, {0})
    >>> sorted(parse_int_set("0..0x0A"))    # Half-open interval with .. or ... or -
    [0, 1, 2, 3, 4, 5, 6, 7, 8, 9]
    >>> sorted(parse_int_set("-9...-5,"))
    [-9, -8, -7, -6]
    >>> sorted(parse_int_set("-9--5; +4, !-8..-5"))     # Exclusion with ! prefix
    [-9, 4]
    >>> sorted(parse_int_set("-10..+10,!-9-+9"))    # Valid separators are , and ;
    [-10, 9]
    >>> sorted(parse_int_set("6-6"))
    []
    >>> sorted(parse_int_set("[1,53,78]"))
    [1, 53, 78]
    >>> parse_int_set("123,456,9-") # doctest:+IGNORE_EXCEPTION_DETAIL
    Traceback (most recent call last):
    ...
    ValueError: ...
    """

    def try_parse(val: str) -> int | None:
        try:
            return int(val, 0)
        except ValueError:
            return None

    incl: set[int] = set()
    excl: set[int] = set()
    for item in _RE_SPLIT.split(_RE_JSON_LIST.sub(r"\1", text)):
        item = item.strip()
        if not item:
            continue
        if item.startswith("!"):
            target_set = excl
            item = item[1:]
        else:
            target_set = incl
        x = try_parse(item)
        if x is not None:
            target_set.add(x)
            continue
        match = _RE_RANGE.match(item)
        if match:
            lo, hi = map(try_parse, match.groups())
            if lo is not None and hi is not None:
                target_set |= set(range(lo, hi))
                continue
        raise ValueError(f"Item {item!r} of the integer set {text!r} could not be parsed")

    result: set[int] | int = incl - excl
    assert isinstance(result, set)
    _logger.debug("Int set %r parsed as %r", text, result)
    return result


class Mode(IntEnum):
    OPERATIONAL = 0
    INITIALIZATION = 1
    MAINTENANCE = 2
    SOFTWARE_UPDATE = 3


class Health(IntEnum):
    NOMINAL = 0
    ADVISORY = 1
    CAUTION = 2
    WARNING = 3


def set_default_usbtingo_env_vars() -> None:
    os.environ.setdefault("UAVCAN__NODE__ID", "126")
    os.environ.setdefault("UAVCAN__CAN__IFACE", "usbtingo:")
    os.environ.setdefault("UAVCAN__CAN__MTU", "64")
    os.environ.setdefault("UAVCAN__CAN__BITRATE", "1000000 5000000")


def spaces_to_padding(text: str) -> Padding:
    """Convert leading and trailing spaces in a string to padding.

    Args:
        text: The input string that may contain leading and/or trailing spaces

    Returns:
        A Padding object with the text content, with leading and trailing spaces converted to padding
    """
    trailing_spaces = len(text) - len(text.rstrip())
    leading_spaces = len(text) - len(text.lstrip())
    return Padding(text.strip(), (0, trailing_spaces, 0, leading_spaces))


_RE_JSON_LIST = re.compile(r"^\s*\[([^]]*)]\s*$")
_RE_SPLIT = re.compile(r"[,;]")
_RE_RANGE = re.compile(r"([+-]?\w+)(?:-|\.\.\.?)([+-]?\w+)")
