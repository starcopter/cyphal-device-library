from typing import Annotated

import typer
from dotenv import load_dotenv

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
    reload: Annotated[bool, typer.Option("--reload", "-r", help="Reload environment from .env file")] = False,
    pnp: Annotated[
        bool, typer.Option("--pnp-server", "-p", help="Launch PnP server for dynamic node ID allocation.")
    ] = False,
):
    if reload:
        load_dotenv(override=True)
    configure_logging(verbosity)
    set_default_usbtingo_env_vars()
