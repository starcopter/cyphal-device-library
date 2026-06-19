"""Helpers for local pycyphal node register maps."""

from __future__ import annotations

from typing import Any, Callable

from pycyphal.application.register import Natural16, Natural32, String

from ..registry import NativeValue


def configure_can_registers(
    registry: Any,
    interface: str,
    *,
    bitrate: int | list[int] | None = None,
    mtu: int | None = None,
) -> None:
    """Populate standard CAN transport registers on a local node registry.

    Sets ``uavcan.can.iface``, ``uavcan.can.mtu``, and ``uavcan.can.bitrate`` so
    :func:`pycyphal.application.make_transport` can open the bus when the node starts.
    Bitrate/MTU defaults follow the same classic-CAN vs CAN-FD rules as
    :func:`~cyphal_device_library.util.make_can_transport`.

    Args:
        registry: Local pycyphal registry from :func:`pycyphal.application.make_registry`.
        interface: CAN interface string (e.g. ``"socketcan:can0"``, ``"virtual:"``).
        bitrate: Classic bitrate (``int``) or FD arbitration/data pair (``list[int]``).
            Defaults to ``[1_000_000, 5_000_000]`` when omitted.
        mtu: Frame MTU override. Defaults to ``8`` (classic) or ``64`` (FD).

    Example::

        registry = pycyphal.application.make_registry()
        configure_can_registers(registry, "virtual:", bitrate=1_000_000, mtu=8)
    """
    resolved_bitrate: list[int]
    resolved_mtu: int
    if bitrate is None:
        # Default to CAN-FD bitrates when caller does not specify transport parameters.
        resolved_bitrate = [1_000_000, 5_000_000]
        resolved_mtu = mtu if mtu is not None else 64
    elif isinstance(bitrate, int):
        resolved_bitrate = [bitrate, bitrate]
        resolved_mtu = mtu if mtu is not None else 8
    else:
        resolved_bitrate = list(bitrate)
        resolved_mtu = mtu if mtu is not None else 64

    registry["uavcan.can.iface"] = String(interface)
    registry["uavcan.can.mtu"] = Natural16([resolved_mtu])
    registry["uavcan.can.bitrate"] = Natural32(resolved_bitrate)


def apply_native_register_overrides(
    registry: Any,
    overrides: dict[str, NativeValue],
) -> None:
    """Apply JSON/native register overrides onto a local pycyphal node registry.

    Uses the same :data:`~cyphal_device_library.registry.NativeValue` types as
    :class:`~cyphal_device_library.registry.Registry` client access. Registers that
    are not already present in the local registry are skipped.

    Args:
        registry: Local pycyphal node registry (after defaults are installed).
        overrides: Register name → native Python value (scalar or list).

    Example::

        apply_native_register_overrides(node.registry, {
            "bms.measurement.vbat.gain": 1.5,
            "motor.inductance_dq": [1e-5, 1e-5],
        })
    """
    for name, value in overrides.items():
        if name not in registry or value is None:
            continue
        current = registry[name]
        # Match the existing register Value type rather than guessing from Python type alone.
        if hasattr(current, "natural8"):
            registry[name] = type(current)(_coerce_scalar_list(value, int))
        elif hasattr(current, "natural16"):
            registry[name] = type(current)(_coerce_scalar_list(value, int))
        elif hasattr(current, "natural32"):
            registry[name] = type(current)(_coerce_scalar_list(value, int))
        elif hasattr(current, "real32"):
            registry[name] = type(current)(_coerce_scalar_list(value, float))
        elif hasattr(current, "string"):
            registry[name] = type(current)(str(value))


def _coerce_scalar_list(value: NativeValue, converter: Callable[[Any], Any]) -> list[Any]:
    """Normalize a scalar or list native value into a pycyphal register array."""
    if isinstance(value, list):
        return [converter(item) for item in value]
    return [converter(value)]
