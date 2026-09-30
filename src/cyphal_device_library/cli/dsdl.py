import typer

from ..util.dsdl import download_and_compile_dsdl_repositories, ensure_dsdl_compiled

app = typer.Typer()


@app.command()
def install(
    force: bool = typer.Option(False, "--force", "-f", help="Force re-download of DSDL repositories"),
) -> None:
    """Download and install default DSDL namespaces."""
    ensure_dsdl_compiled(force=force)
    download_and_compile_dsdl_repositories(force=force)
    typer.echo("DSDL repositories successfully installed")
