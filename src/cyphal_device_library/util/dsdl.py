import io
import itertools
import logging
import os
import shutil
import site
import sys
import tempfile
import tomllib
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class DSDLRepository:
    zip_url: str
    namespaces: list[str]

    def download(self, output_directory: Path, force: bool = False) -> None:
        if not force and all((output_directory / namespace).is_dir() for namespace in self.namespaces):
            logger.debug("Repository %s already downloaded, skipping", self.zip_url)
            return

        logger.debug("Downloading repository %s to %s", self.zip_url, output_directory)
        with urllib.request.urlopen(self.zip_url) as response:
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
            output_directory.mkdir(parents=True, exist_ok=True)

            for namespace in self.namespaces:
                shutil.move(repo_path / namespace, output_directory / namespace)


def get_repositories() -> list[DSDLRepository]:
    with open(Path(__file__).parent / "repositories.toml", "rb") as f:
        data = tomllib.load(f)

    return [DSDLRepository(zip_url=repo["zip"], namespaces=repo["namespaces"]) for repo in data.values()]


def get_output_directory() -> Path:
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


def get_default_dsdl_dir() -> Path:
    if os.name == "nt":  # Windows
        cache_root = Path(os.environ.get("LOCALAPPDATA", "~/.cache"))
    else:  # Unix-like
        cache_root = Path("~/.cache")

    return cache_root.expanduser() / "dsdl"


def download_dsdl_repositories(
    repositories: list[DSDLRepository] | None = None,
    dsdl_directory: Path | None = None,
    force: bool = False,
) -> None:
    repositories = repositories or get_repositories()
    dsdl_directory = dsdl_directory or get_default_dsdl_dir()

    for repo in repositories:
        repo.download(dsdl_directory, force=force)


def download_and_compile_dsdl_repositories(
    repositories: list[DSDLRepository] | None = None,
    output_directory: str | Path | None = None,
    force: bool = False,
) -> None:
    import pycyphal.dsdl

    repositories = repositories or get_repositories()

    if output_directory is None:
        output_directory = get_output_directory()
        logger.debug("Using %s as output directory", output_directory)
    output_directory = Path(output_directory).resolve()

    if not force and all(
        (output_directory / namespace / "__init__.py").is_file()
        for repo in repositories
        for namespace in repo.namespaces
    ):
        logger.debug("All namespaces already exist, skipping")
    else:
        dsdl_root = get_default_dsdl_dir()
        download_dsdl_repositories(repositories, dsdl_directory=dsdl_root, force=force)

        flat_namespaces = list(itertools.chain.from_iterable(repo.namespaces for repo in repositories))
        logger.info("Installing namespaces %s to %s", flat_namespaces, output_directory)
        pycyphal.dsdl.compile_all(
            [dsdl_root / namespace for namespace in flat_namespaces],
            output_directory,
        )

    if output_directory not in [Path(p).resolve() for p in sys.path]:
        sys.path.append(str(output_directory))


def update_cyphal_path(dsdl_directory: Path) -> None:
    cyphal_path_str = os.environ.get("CYPHAL_PATH", "").replace(os.pathsep, ";").split(";")
    cyphal_path = [d for d in cyphal_path_str if d.strip()]  # filter out empty strings
    if str(dsdl_directory) not in cyphal_path:
        cyphal_path.append(str(dsdl_directory))
        logger.info("Adding %s to CYPHAL_PATH", dsdl_directory)
        os.environ["CYPHAL_PATH"] = os.pathsep.join(cyphal_path)


if os.environ.get("CYPHAL_DEVICE_LIBRARY_NO_DSDL_DOWNLOAD", "False").lower() not in ("true", "1", "t", "yes", "y"):
    logger.debug("Downloading DSDL repositories.")
    _dsdl_directory = get_default_dsdl_dir()
    download_dsdl_repositories(dsdl_directory=_dsdl_directory)
    update_cyphal_path(_dsdl_directory)
else:
    logger.debug("DSDL repositories download skipped.")
