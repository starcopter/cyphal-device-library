import logging
from typing import Annotated

import typer
from rich.logging import RichHandler

from cyphal_device_library.util.dsdl import download_and_compile_dsdl_repositories

app = typer.Typer()


def _configure_logging(verbose: int) -> None:
    logging.basicConfig(
        level={
            0: logging.WARNING,
            1: logging.INFO,
            2: logging.DEBUG,
        }.get(verbose, logging.NOTSET),
        format="%(message)s",
        datefmt="[%X]",
        handlers=[RichHandler()],
    )


@app.callback()
def main(
    verbose: Annotated[int, typer.Option("--verbose", "-v", count=True)] = 0,
):
    pass


@app.command()
def install_dsdl(
    ctx: typer.Context,
    force: bool = typer.Option(False, "--force", "-f", help="Force re-download of DSDL repositories"),
) -> None:
    """Download and install default DSDL namespaces."""
    _configure_logging(ctx.parent.params["verbose"])

    download_and_compile_dsdl_repositories(force=force)
    typer.echo("DSDL repositories successfully installed")
