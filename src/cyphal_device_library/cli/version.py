import typer
from ..util.dsdl import get_output_directory
import os
import datetime
import subprocess

app = typer.Typer()
def get_latest_git_tag():
    try:
        tag = subprocess.check_output(
            ["git", "describe", "--tags", "--abbrev=0"],
            cwd=os.path.dirname(__file__),
            stderr=subprocess.DEVNULL,
            text=True
        ).strip()
        print(os.path.dirname(__file__))
        return tag
    except Exception:
        return "unknown"

__version__ = get_latest_git_tag()

@app.command()
def version():
    """Print the version of the CLI."""
    typer.echo(__version__)

    typer.echo(os.path.dirname(__file__))

    # Print the last modified time of the dsdl_dir
    dsdl_dir = get_output_directory()
    if os.path.exists(dsdl_dir):
        mtime = os.path.getmtime(dsdl_dir)
        last_updated = datetime.datetime.fromtimestamp(mtime)
        typer.echo(f"dsdl_dir last modified: {last_updated.strftime('%Y-%m-%d %H:%M:%S')}")
    else:
        typer.echo("dsdl_dir does not exist.")


if __name__ == "__main__":
    app()