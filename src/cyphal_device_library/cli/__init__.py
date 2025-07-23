import importlib.metadata
import logging
from datetime import datetime
from typing import Annotated

import typer
from dotenv import load_dotenv

from ..util.dsdl import get_output_directory
from ..util.logging import UAVCAN_SEVERITY_TO_PYTHON
from . import dsdl
from ._util import configure_logging, set_default_usbtingo_env_vars

app = typer.Typer()
app.add_typer(dsdl.app)

try:
    from . import discover, update

    app.add_typer(update.app)
    app.add_typer(discover.app)
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
):
    if reload:
        load_dotenv(override=True)
    configure_logging(verbosity)
    set_default_usbtingo_env_vars()

    diagnost_record_logger = logging.getLogger("uavcan.diagnostic.record")
    diagnost_record_logger.setLevel(UAVCAN_SEVERITY_TO_PYTHON[7 - diagnostic_record_verbosity])
    level_name = logging.getLevelName(diagnost_record_logger.level)
    print(f"[-d|--diagnostic] Setting diagnostic record verbosity to {level_name}")

    # Suppress heartbeat publisher errors (e.g. no cyphal node receive CAN messages)
    class HeartbeatExceptionFilter(logging.Filter):
        def filter(self, record):
            if "publisher task exception:" in record.getMessage():
                if "[Errno 105] No buffer space available" in record.getMessage():
                    # print("[Errno 105] Heartbeat Publish: no buffer space available")
                    return False
            return True

    heartbeat_publish_logger = logging.getLogger("pycyphal.application.heartbeat_publisher")
    heartbeat_publish_logger.addFilter(HeartbeatExceptionFilter())

    # Suppress publisher port errors (e.g. no cyphal node receive CAN messages)
    class PresentationPublishFilter(logging.Filter):
        def filter(self, record):
            if "deferred publication has failed" in record.getMessage():
                if "[Errno 105] No buffer space available" in record.getMessage():
                    # print("[Errno 105] Presentation Layer Publish: no buffer space available")
                    return False
            return True

    presentation_publisher_logger = logging.getLogger("pycyphal.presentation._port._publisher")
    presentation_publisher_logger.addFilter(PresentationPublishFilter())


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
