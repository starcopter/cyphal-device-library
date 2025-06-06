from typing import Annotated

import typer

from . import discover, dsdl, update
from ._util import configure_logging, set_default_usbtingo_env_vars

app = typer.Typer()
app.add_typer(dsdl.app)
app.add_typer(update.app)
app.add_typer(discover.app)


@app.callback()
def main(
    verbosity: Annotated[int, typer.Option("--verbose", "-v", count=True)] = 0,
):
    configure_logging(verbosity)
    set_default_usbtingo_env_vars()
