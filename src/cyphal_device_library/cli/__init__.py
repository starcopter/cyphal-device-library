from typing import Annotated

import typer

from . import dsdl, update
from ._util import configure_logging

app = typer.Typer()
app.add_typer(dsdl.app)
app.add_typer(update.app)


@app.callback()
def main(
    verbosity: Annotated[int, typer.Option("--verbose", "-v", count=True)] = 0,
):
    configure_logging(verbosity)
