import io
import itertools
import logging
import shutil
import site
import sys
import tempfile
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path

import pycyphal.dsdl

logger = logging.getLogger(__name__)


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


def get_dsdl_path() -> Path:
    for path in map(Path, site.getsitepackages()):
        try:
            test_file = path / ".write_test"
            test_file.touch()
            test_file.unlink()
            return path
        except (OSError, PermissionError):
            logger.debug("Skipping %s because it is not writable", path)
            continue

    # use user site-packages instead
    # https://docs.python.org/3/library/site.html#module-usercustomize
    dsdl_path = Path(site.getusersitepackages())

    if dsdl_path.resolve() not in [Path(p).resolve() for p in sys.path]:
        sys.path.append(str(dsdl_path))

    return dsdl_path


def download_and_compile_dsdl(
    repositories: list[DSDLRepository] = [PUBLIC_REGULATED_DATA_TYPES, STARCROPTER_DSDL, ZUBAX_DSDL],
    output_directory: str | Path | None = None,
    force: bool = False,
) -> None:
    """Download Cyphal DSDL repositories and compile the DSDL files."""
    if output_directory is None:
        output_directory = get_dsdl_path()
        logger.debug("Using %s as output directory", output_directory)
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
            logger.info("Installing namespaces %s to %s", flat_namespaces, output_directory)
            pycyphal.dsdl.compile_all(
                [dsdl_root / namespace for namespace in flat_namespaces],
                output_directory,
            )

    if output_directory not in [Path(p).resolve() for p in sys.path]:
        sys.path.append(str(output_directory))


if __name__ == "__main__":
    from . import configure_logging

    configure_logging()
    download_and_compile_dsdl()
