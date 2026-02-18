import importlib.metadata
import logging
from datetime import datetime
from typing import Annotated

import typer
from dotenv import load_dotenv

from ..util.dsdl import get_output_directory
from ..util.logging import UAVCAN_SEVERITY_TO_PYTHON, Errno105Filter
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
    can_bitrate: Annotated[
        int, typer.Option(help="CAN bitrate in bits per second. Defaults to 1,000,000.")
    ] = 1_000_000,
    can_fd_bitrate: Annotated[
        list[int], typer.Option(help="CAN FD bitrates in bits per second. Defaults to [1,000,000, 5,000,000].")
    ] = [1_000_000, 5_000_000],
):
    if reload:
        load_dotenv(override=True)
    configure_logging(verbosity)
    set_default_usbtingo_env_vars()

    diagnost_record_logger = logging.getLogger("uavcan.diagnostic.record")
    diagnost_record_logger.setLevel(UAVCAN_SEVERITY_TO_PYTHON[7 - diagnostic_record_verbosity])
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
