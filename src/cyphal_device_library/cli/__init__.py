import importlib.metadata
import logging
from typing import Annotated

import typer
from dotenv import load_dotenv

from ..util.logging import UAVCAN_SEVERITY_TO_PYTHON, Errno105Filter
from . import discover, registry, update
from ._util import configure_logging, set_default_usbtingo_env_vars

app = typer.Typer()
app.add_typer(update.app)
app.add_typer(discover.app)
app.add_typer(registry.app)


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


@app.command()
def install(
    force: bool = typer.Option(False, "--force", "-f", help="Force re-download of DSDL repositories"),
) -> None:
    """Deprecated command to download and install DSDL namespaces."""
    typer.echo(
        "This command is deprecated and has no effect. Run `uv sync --upgrade-package sc-packaged-dsdl`, "
        "`uv tool upgrade cyphal` or equivalent to upgrade DSDL data types."
    )
