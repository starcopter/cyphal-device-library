import asyncio
import io
import itertools
import logging
import os
import shutil
import sys
import tempfile
import urllib.request
import zipfile
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import TypeVar

import pycyphal.dsdl
import rich.console
import rich.prompt

logger = logging.getLogger(__name__)
T = TypeVar("T")


@dataclass
class DSDLRepository:
    url: str
    namespaces: list[str]

    def download(self, output_directory: str | Path) -> None:
        logger.debug("Downloading repository %s", self.url)

        with urllib.request.urlopen(self.url) as response:
            zip_data = io.BytesIO(response.read())

        with tempfile.TemporaryDirectory() as temp_dir:
            with zipfile.ZipFile(zip_data) as zip_ref:
                zip_ref.extractall(temp_dir)

            directories = [path for path in Path(temp_dir).iterdir() if path.is_dir()]
            if len(directories) != 1:
                raise RuntimeError(
                    f"Expected exactly one directory in the zip file, got {len(directories)}:\n{directories}"
                )

            repo_path = directories[0]

            for namespace in self.namespaces:
                shutil.move(repo_path / namespace, output_directory / namespace)


PUBLIC_REGULATED_DATA_TYPES = DSDLRepository(
    url="https://github.com/OpenCyphal/public_regulated_data_types/archive/refs/heads/master.zip",
    namespaces=["uavcan", "reg"],
)

STARCROPTER_DSDL = DSDLRepository(
    url="https://github.com/starcopter/starcopter-dsdl/archive/refs/heads/main.zip",
    namespaces=["starcopter"],
)

ZUBAX_DSDL = DSDLRepository(
    url="https://github.com/Zubax/zubax_dsdl/archive/refs/heads/master.zip",
    namespaces=["zubax"],
)


def get_venv_path() -> Path:
    def _find_upwards(path: str | Path, dirname: str = ".venv") -> Path | None:
        path = Path(path)
        while path != path.root:
            if path.name == dirname and path.is_dir():
                return path
            path = path.parent
        return None

    def _find_first_dir(paths: list[str | Path], dirname: str = ".venv") -> Path | None:
        for path in map(Path, paths):
            if path.name == dirname and path.is_dir():
                return path
        return None

    venv = os.environ.get("VIRTUAL_ENV") or _find_upwards(sys.executable) or _find_first_dir(sys.path)
    if not venv:
        raise RuntimeError("Virtual environment not found")

    return Path(venv)


def download_and_compile_dsdl(
    repositories: list[DSDLRepository] = [PUBLIC_REGULATED_DATA_TYPES, STARCROPTER_DSDL, ZUBAX_DSDL],
    output_directory: str | Path | None = None,
    force: bool = False,
) -> None:
    """Download Cyphal DSDL repositories and compile the DSDL files."""
    if output_directory is None:
        output_directory = get_venv_path()
    output_directory = Path(output_directory).resolve()

    if not force and all(
        (output_directory / namespace / "__init__.py").is_file()
        for repo in repositories
        for namespace in repo.namespaces
    ):
        logger.debug("All namespaces already exist, skipping")
    else:
        with tempfile.TemporaryDirectory() as _temp_dir:
            dsdl_root = Path(_temp_dir)

            for repo in repositories:
                repo.download(dsdl_root)

            flat_namespaces = list(itertools.chain.from_iterable(repo.namespaces for repo in repositories))
            pycyphal.dsdl.compile_all(
                [dsdl_root / namespace for namespace in flat_namespaces],
                output_directory,
            )

    if output_directory not in [Path(p).resolve() for p in sys.path]:
        sys.path.append(str(output_directory))


def configure_logging(console: rich.console.Console | None = None, filename: Path | str | None = None):
    from rich.logging import RichHandler

    root_logger = logging.getLogger()
    root_logger.setLevel("DEBUG")
    logging.getLogger("pycyphal").setLevel("INFO")
    logging.getLogger("pydsdl").setLevel("INFO")
    logging.getLogger("nunavut").setLevel("INFO")

    rich_handler = RichHandler(console=console, rich_tracebacks=True, tracebacks_show_locals=True, show_path=False)
    rich_handler.setFormatter(logging.Formatter("%(name)-20s %(message)s", datefmt="[%X]"))
    root_logger.addHandler(rich_handler)

    if filename:
        file_handler = logging.FileHandler(filename)
        file_handler.setFormatter(
            logging.Formatter(
                "%(asctime)s.%(msecs)03d %(name)-30s %(levelname)-8s: %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
        root_logger.addHandler(file_handler)


async def async_prompt(prompt: rich.prompt.PromptBase[T], default: T = ...) -> T:
    return await asyncio.get_event_loop().run_in_executor(None, partial(prompt, default=default))


if __name__ == "__main__":
    configure_logging()
    # download_and_install_standard_uavcan_namespace()
