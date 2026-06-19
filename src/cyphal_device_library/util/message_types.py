"""Load Cyphal DSDL message types from register type name strings."""

from __future__ import annotations

import importlib
import re
from typing import TypeVar

MessageClass = TypeVar("MessageClass")

_DSDL_TYPE_RE = re.compile(
    r"(?P<namespace>[a-zA-Z_][a-zA-Z0-9_]*(\.[a-zA-Z_][a-zA-Z0-9_]*)*)\."
    r"(?P<shortname>[a-zA-Z_][a-zA-Z0-9_]*)\.(?P<major>\d+)\.(?P<minor>\d+)"
)


def load_message_type(type_name: str) -> type[MessageClass]:
    """Resolve a DSDL type name string to its Python message class.

    Args:
        type_name: Full DSDL name, e.g. ``starcopter.highdra.bms.PowerData.0.2``.

    Raises:
        ValueError: If the type name format is invalid.
        RuntimeError: If the type cannot be imported.
    """
    match = _DSDL_TYPE_RE.fullmatch(str(type_name).strip())
    if not match:
        raise ValueError(f"Invalid DSDL type name: {type_name!r}")

    try:
        namespace = importlib.import_module(match.group("namespace"))
        py_type_name = "_".join(
            match.group("shortname", "major")
            if int(match.group("major")) > 0
            else match.group("shortname", "major", "minor")
        )
        return getattr(namespace, py_type_name)
    except (ImportError, AttributeError) as exc:
        raise RuntimeError(f"No matching type found for {type_name!r}") from exc
