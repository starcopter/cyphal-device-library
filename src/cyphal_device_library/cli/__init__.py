import importlib.metadata
import logging
import sys
from datetime import datetime
from typing import Annotated

import rich
import typer
from dotenv import load_dotenv

from ..util._logging import UAVCAN_SEVERITY_TO_PYTHON, Errno105Filter, UAVCANDiagnosticSeverity
from ..util.dsdl import get_output_directory
from . import dsdl
from ._util import configure_logging, set_default_usbtingo_env_vars

app = typer.Typer()
app.add_typer(dsdl.app)

try:
    from . import discover, registry, update

    app.add_typer(update.app)
    app.add_typer(discover.app)
    app.add_typer(registry.app)
except ImportError:
    app.info.epilog = "Run `cyphal install` to make more commands available."


def general_argument_position_reorder(argv: list[str]) -> list[str]:
    if len(argv) <= 2:
        return argv

    global_no_value = {
        "-v",
        "-vv",
        "-vvv",
        "-vvvv",
        "--verbose",
        "-r",
        "--reload",
        "-p",
        "--pnp-server",
    }
    global_with_value = {
        "-d",
        "--diagnostic",
        "--interface",
        "--can-protocol",
        "--cyphal-node-id",
        "--can-arb-bitrate",
        "--can-data-bitrate",
    }

    global_args: list[str] = []
    command_args: list[str] = []

    index = 1
    while index < len(argv):
        arg = argv[index]

        if arg == "--":
            command_args.extend(argv[index:])
            break

        if arg in global_no_value:
            if arg in global_args:
                rich.print(f"[yellow]Duplicate global argument '{arg}' found. Ignoring duplicates.[/yellow]")
            else:
                global_args.append(arg)
            index += 1
            continue

        if arg in global_with_value:
            if arg in global_args:
                rich.print(f"[yellow]Duplicate global argument '{arg}' found. Ignoring duplicates.[/yellow]")
            else:
                global_args.append(arg)
                if index + 1 < len(argv):
                    global_args.append(argv[index + 1])
                else:
                    rich.print(f"[red]Expected a value after global argument '{arg}', but none was found.[/red]")
                    sys.exit(1)
            index += 2
            continue

        command_args.append(arg)
        index += 1

    return [argv[0], *global_args, *command_args]


def run() -> None:
    reordered_argv = general_argument_position_reorder(sys.argv)
    if reordered_argv != sys.argv:
        sys.argv[:] = reordered_argv
    app()


@app.callback()
def main(
    verbosity: Annotated[int, typer.Option("--verbose", "-v", count=True)] = 0,
    diagnostic_record_verbosity: Annotated[
        int,
        typer.Option(
            "--diagnostic",
            "-d",
            help="Set the logging level for Cyphal DiagnosticRecords, e.g. in discovery",
        ),
    ] = 3,
    reload: Annotated[bool, typer.Option("--reload", "-r", help="Reload environment from .env file")] = False,
    pnp: Annotated[
        bool, typer.Option("--pnp-server", "-p", help="Launch PnP server for dynamic node ID allocation.")
    ] = False,
    interface: Annotated[
        str | None,
        typer.Option(
            help="CAN interface to be used. If not provided, the user will be asked to select one.",
        ),
    ] = None,
    can_protocol: Annotated[
        str | None,
        typer.Option(
            help="CAN protocol to use ('classic' or 'fd'). If not provided, the user will be asked to select one.",
        ),
    ] = None,
    cyphal_node_id: Annotated[int, typer.Option(help="Node ID to use for this client. Defaults to 127.")] = 127,
    can_arb_bitrate: Annotated[
        int,
        typer.Option(
            help="CAN arbitration bitrate in bits per second. (used for all by Classic CAN) Defaults to 1,000,000."
        ),
    ] = 1_000_000,
    can_data_bitrate: Annotated[
        int, typer.Option(help="CAN data bitrates in bits per second. (only used by CAN FD) Defaults to 5,000,000.")
    ] = 5_000_000,
):
    if reload:
        load_dotenv(override=True)
    configure_logging(verbosity)
    set_default_usbtingo_env_vars()

    diagnost_record_logger = logging.getLogger("uavcan.diagnostic.record")
    severity = UAVCANDiagnosticSeverity(max(0, min(7, 7 - diagnostic_record_verbosity)))
    diagnost_record_logger.setLevel(UAVCAN_SEVERITY_TO_PYTHON[severity])
    level_name = logging.getLevelName(diagnost_record_logger.level)
    logging.debug(f"[-d|--diagnostic] Setting diagnostic record verbosity to {level_name}")

    Errno105Filter.apply_to("pycyphal.application")
    Errno105Filter.apply_to("pycyphal.presentation")


@app.command()
def version():
    """Print the version of the CLI."""

    __version__ = importlib.metadata.version("cyphal-device-library")

    typer.echo(f"This is cyphal-device-library version {__version__}")

    dsdl_dir = get_output_directory()
    if dsdl_dir.is_dir():
        last_updated = datetime.fromtimestamp(dsdl_dir.stat().st_ctime).replace(microsecond=0)
        typer.echo(f"DSDL files compiled to {dsdl_dir}, last updated {last_updated}")
    else:
        typer.echo("DSDL files not compiled yet. Run `cyphal install` to compile them.")
