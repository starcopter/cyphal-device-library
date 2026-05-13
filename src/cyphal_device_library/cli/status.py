import importlib.metadata
import os
import platform
import sys
from pathlib import Path
from typing import Annotated, TypedDict

import rich
import rich.console
import rich.table
import typer
from can import Bus, BusState, CanTimeoutError, Message

from ..util import SUPPORTED_CAN_INTERFACES, list_available_can_channels

app = typer.Typer(invoke_without_command=True)


class ProbeRow(TypedDict):
    channel: str
    mode: str
    success: int
    timeout: int
    errors: int
    state: str
    open_error: str


def _probe_can_tx_modes(
    channels: list[str],
    attempts: int,
    send_timeout: float,
    arb_bitrate: int,
    data_bitrate: int,
) -> list[ProbeRow]:
    results: list[ProbeRow] = []

    mode_config = ["fd_brs_off", "fd_brs_on", "classic"]

    for channel in channels:
        for mode in mode_config:
            success = 0
            timeout = 0
            errors = 0
            state = "unknown"
            bus = None
            open_error = ""

            try:
                if mode == "classic":
                    bus = Bus(interface="usbtingo", channel=channel, bitrate=arb_bitrate, fd=False)
                    message = Message(
                        arbitration_id=0x123,
                        is_extended_id=False,
                        is_fd=False,
                        bitrate_switch=False,
                        data=b"\x01\x02\x03\x04",
                    )
                else:
                    bus = Bus(
                        interface="usbtingo",
                        channel=channel,
                        bitrate=arb_bitrate,
                        data_bitrate=data_bitrate,
                        fd=True,
                    )
                    message = Message(
                        arbitration_id=0x123,
                        is_extended_id=False,
                        is_fd=True,
                        bitrate_switch=(mode == "fd_brs_on"),
                        data=b"\x01\x02\x03\x04",
                    )

                state = str(getattr(bus, "state", BusState.ACTIVE))

                for _ in range(attempts):
                    try:
                        bus.send(message, timeout=send_timeout)
                        success += 1
                    except CanTimeoutError:
                        timeout += 1
                    except Exception:
                        errors += 1
            except Exception as ex:  # pragma: no cover - depends on host drivers/hardware
                open_error = str(ex)
            finally:
                if bus is not None:
                    bus.shutdown()

            results.append(
                {
                    "channel": channel,
                    "mode": mode,
                    "success": success,
                    "timeout": timeout,
                    "errors": errors,
                    "state": state,
                    "open_error": open_error,
                }
            )

    return results


def _recommend_discover_commands(
    probe_results: list[ProbeRow],
    node_id: int,
    arb_bitrate: int,
    data_bitrate: int,
    attempts: int,
) -> list[str]:
    recommendations: list[str] = []
    channels = sorted({str(entry["channel"]) for entry in probe_results})

    for channel in channels:
        by_mode = {entry["mode"]: entry for entry in probe_results if entry["channel"] == channel}

        fd_on_ok = by_mode.get("fd_brs_on", {"success": 0})["success"] == attempts
        fd_off_ok = by_mode.get("fd_brs_off", {"success": 0})["success"] == attempts
        classic_ok = by_mode.get("classic", {"success": 0})["success"] == attempts

        if fd_on_ok:
            recommendations.append(
                "cyphal discover"
                + f" --can-protocol fd --interface usbtingo:{channel}"
                + f" --cyphal-node-id {node_id} --can-arb-bitrate {arb_bitrate} --can-data-bitrate {data_bitrate}"
            )
            continue

        if classic_ok:
            recommendations.append(
                "cyphal discover"
                + f" --can-protocol classic --interface usbtingo:{channel}"
                + f" --cyphal-node-id {node_id} --can-arb-bitrate {arb_bitrate}"
            )

        if fd_off_ok and not fd_on_ok:
            recommendations.append(
                f"Note for usbtingo:{channel}: FD TX works only with bitrate switch OFF in probe,"
                + " but this CLI discover command currently does not expose a BRS toggle."
            )

        if not fd_on_ok and not fd_off_ok and not classic_ok:
            recommendations.append(
                f"No safe discover arguments identified for usbtingo:{channel} (all probe modes had timeouts/errors)."
            )

    return recommendations


@app.callback()
def status(
    probe: Annotated[
        bool,
        typer.Option(
            "--probe/--no-probe",
            help="Run CAN TX probe (classic + CAN FD with bitrate-switch on/off) and suggest safe command arguments.",
        ),
    ] = True,
    probe_attempts: Annotated[
        int,
        typer.Option(help="Number of send attempts per probe mode/channel."),
    ] = 10,
    probe_send_timeout: Annotated[
        float,
        typer.Option(help="Timeout in seconds for each probe send attempt."),
    ] = 0.2,
    can_arb_bitrate: Annotated[
        int,
        typer.Option(help="Probe arbitration bitrate in bits per second."),
    ] = 1_000_000,
    can_data_bitrate: Annotated[
        int,
        typer.Option(help="Probe CAN FD data bitrate in bits per second."),
    ] = 5_000_000,
):
    """Print diagnostics about the current runtime and CAN setup."""

    rich.print("[bold]Cyphal CLI Status[/bold]")
    rich.print(f"Platform: {platform.platform()}")
    rich.print(f"Python: {sys.version.split()[0]} ({sys.executable})")
    rich.print(f"argv[0]: {sys.argv[0]}")

    launcher = Path(sys.argv[0])
    if launcher.exists():
        rich.print(f"Launcher path: {launcher.resolve()}")
        try:
            with launcher.open("r", encoding="utf-8") as handle:
                first_line = handle.readline().strip()
            if first_line.startswith("#!"):
                rich.print(f"Launcher shebang: {first_line[2:]}")
        except OSError:
            rich.print("Launcher shebang: <unavailable>")

    for package in ["cyphal-device-library", "pycyphal", "python-can", "python-can-usbtingo", "typer"]:
        try:
            rich.print(f"{package}: {importlib.metadata.version(package)}")
        except importlib.metadata.PackageNotFoundError:
            rich.print(f"{package}: <not installed>")

    for key in [
        "VIRTUAL_ENV",
        "PYTHONPATH",
        "UAVCAN__NODE__ID",
        "UAVCAN__CAN__IFACE",
        "UAVCAN__CAN__MTU",
        "UAVCAN__CAN__BITRATE",
    ]:
        rich.print(f"{key}={os.environ.get(key, '')}")

    rich.print(f"Supported CAN interfaces: {', '.join(SUPPORTED_CAN_INTERFACES)}")
    try:
        channels = list_available_can_channels()
    except Exception as ex:  # pragma: no cover - depends on host drivers/hardware
        rich.print(f"Available channels: <detection failed: {ex!s}>")
        channels = []
    else:
        rich.print(f"Available channels: {', '.join(channels) if channels else '<none>'}")

    if not probe:
        return

    rich.print()
    usbtingo_channels = [item.split(":", 1)[1] for item in channels if item.startswith("usbtingo:")]
    if not usbtingo_channels:
        rich.print("Probe result: no usbtingo channels detected.")
        return

    rich.print("[bold]Probe: CAN TX viability[/bold]")
    console = rich.console.Console()
    with console.status("[bold cyan]Probing CAN TX viability...[/bold cyan]", spinner="dots"):
        probe_results = _probe_can_tx_modes(
            channels=usbtingo_channels,
            attempts=probe_attempts,
            send_timeout=probe_send_timeout,
            arb_bitrate=can_arb_bitrate,
            data_bitrate=can_data_bitrate,
        )

    rich.print("[bold]Probe Table[/bold]")
    table = rich.table.Table()
    table.add_column("channel")
    table.add_column("mode")
    table.add_column("success", justify="right")
    table.add_column("timeout", justify="right")
    table.add_column("errors", justify="right")
    table.add_column("state")
    table.add_column("open_error")

    for row in probe_results:
        table.add_row(
            row["channel"],
            row["mode"],
            str(row["success"]),
            str(row["timeout"]),
            str(row["errors"]),
            row["state"],
            row["open_error"] or "-",
        )
    rich.print(table)

    node_id_raw = os.environ.get("UAVCAN__NODE__ID", "127")
    try:
        node_id = int(node_id_raw)
    except ValueError:
        node_id = 127

    rich.print("[bold]Suggested Safe Discover Commands[/bold]")
    for suggestion in _recommend_discover_commands(
        probe_results,
        node_id=node_id,
        arb_bitrate=can_arb_bitrate,
        data_bitrate=can_data_bitrate,
        attempts=probe_attempts,
    ):
        rich.print(suggestion)
