import io
import itertools
import json
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

_STAMP_NAME = ".cyphal-dsdl-stamp.json"
_DOWNLOAD_DISABLED_VALUES = {"true", "1", "t", "yes", "y"}


@dataclass
class DSDLRepository:
    zip_url: str
    namespaces: list[str]

    def download(self, output_directory: Path, force: bool = False) -> None:
        if not force and all((output_directory / namespace).is_dir() for namespace in self.namespaces):
            logger.info("Repository %s already downloaded, skipping", self.zip_url)
            return

        logger.info("Downloading repository %s...", self.zip_url)
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
                src = repo_path / namespace
                dest = output_directory / namespace
                if dest.exists():
                    shutil.rmtree(dest)
                shutil.move(src, dest)
                logger.info("Extracted namespace '%s' to %s", namespace, dest)


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
        except OSError, PermissionError:
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
        logger.info("All namespaces already exist, skipping")
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


def dsdl_updates_disabled() -> bool:
    """Return whether automatic DSDL downloads are switched off."""
    return os.environ.get("CYPHAL_DEVICE_LIBRARY_NO_DSDL_DOWNLOAD", "False").lower() in _DOWNLOAD_DISABLED_VALUES


def _directory_is_writable(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".write_test"
        probe.touch()
        probe.unlink()
    except OSError:
        return False
    return True


def get_shared_compiled_directory() -> Path:
    """Return the compiled DSDL directory shared by every Python environment.

    pycyphal's import hook reads ``PYCYPHAL_PATH`` and otherwise uses ``~/.pycyphal``.
    Software-checkout scripts run in a fresh uv environment, so they cannot see
    packages compiled into the highdra CLI's site-packages. Compiling here makes
    one tree available to the CLI and to those scripts. When ``~/.pycyphal`` is
    not writable, the compile goes to ``~/.cache/pycyphal`` instead.
    """
    configured = os.environ.get("PYCYPHAL_PATH", "").strip()
    if configured:
        return Path(configured).expanduser()
    default = Path.home() / ".pycyphal"
    if _directory_is_writable(default):
        return default
    return Path.home() / ".cache" / "pycyphal"


def _remote_fingerprints(repositories: list[DSDLRepository]) -> dict[str, str]:
    """Return an upstream identity for each repository archive."""
    fingerprints: dict[str, str] = {}
    for repo in repositories:
        request = urllib.request.Request(repo.zip_url, method="HEAD")
        with urllib.request.urlopen(request, timeout=30) as response:
            etag = response.headers.get("ETag")
            if etag:
                fingerprints[repo.zip_url] = etag
                continue
            last_modified = response.headers.get("Last-Modified") or ""
            length = response.headers.get("Content-Length") or ""
            fingerprints[repo.zip_url] = f"{response.geturl()}|{last_modified}|{length}"
    return fingerprints


def _sources_present(dsdl_directory: Path, repositories: list[DSDLRepository]) -> bool:
    return all((dsdl_directory / namespace).is_dir() for repo in repositories for namespace in repo.namespaces)


def _namespaces_compiled(output_directory: Path, repositories: list[DSDLRepository]) -> bool:
    return all(
        (output_directory / namespace / "__init__.py").is_file()
        for repo in repositories
        for namespace in repo.namespaces
    )


def _read_stamp(output_directory: Path) -> dict[str, object]:
    path = output_directory / _STAMP_NAME
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError, json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def _write_stamp(output_directory: Path, fingerprints: dict[str, str]) -> None:
    path = output_directory / _STAMP_NAME
    path.write_text(json.dumps({"fingerprints": fingerprints}, sort_keys=True), encoding="utf-8")


def _remove_compiled_namespaces(output_directory: Path, repositories: list[DSDLRepository]) -> None:
    for repo in repositories:
        for namespace in repo.namespaces:
            destination = output_directory / namespace
            if destination.exists():
                shutil.rmtree(destination)


def _compile_namespaces(
    dsdl_directory: Path,
    output_directory: Path,
    repositories: list[DSDLRepository],
) -> None:
    import pycyphal.dsdl

    flat_namespaces = list(itertools.chain.from_iterable(repo.namespaces for repo in repositories))
    output_directory.mkdir(parents=True, exist_ok=True)
    _remove_compiled_namespaces(output_directory, repositories)
    logger.warning("Compiling Cyphal DSDL namespaces %s into %s", flat_namespaces, output_directory)
    pycyphal.dsdl.compile_all(
        [dsdl_directory / namespace for namespace in flat_namespaces],
        output_directory,
    )


def _prepend_sys_path(directory: Path) -> None:
    resolved = str(directory.resolve())
    sys.path[:] = [path for path in sys.path if path and str(Path(path).resolve()) != resolved]
    sys.path.insert(0, resolved)


def ensure_dsdl_compiled(
    repositories: list[DSDLRepository] | None = None,
    dsdl_directory: Path | None = None,
    output_directory: Path | None = None,
    force: bool = False,
) -> Path:
    """Download DSDL when upstream archives change and compile the shared package tree.

    ``cyphal install`` writes into the current interpreter's site-packages. A new
    highdra install, and each software-checkout ``uv run`` environment, does not
    have that tree. This function keeps ``PYCYPHAL_PATH`` (or ``~/.pycyphal``)
    current and puts it on ``sys.path``. It also sets ``CYPHAL_PATH`` to the
    downloaded sources so pycyphal's import hook can load the same packages.

    Args:
        repositories: Repositories to install. Defaults to ``repositories.toml``.
        dsdl_directory: Source cache. Defaults to ``~/.cache/dsdl``.
        output_directory: Compiled output. Defaults to the shared pycyphal directory.
        force: Re-download and recompile even when the upstream fingerprint matches.

    Returns:
        Resolved compiled output directory.
    """
    repositories = repositories or get_repositories()
    dsdl_directory = (dsdl_directory or get_default_dsdl_dir()).resolve()
    output_directory = (output_directory or get_shared_compiled_directory()).resolve()

    sources_ready = _sources_present(dsdl_directory, repositories)
    compiled_ready = _namespaces_compiled(output_directory, repositories)
    stamp = _read_stamp(output_directory)
    fingerprints: dict[str, str] | None
    try:
        fingerprints = None if force else _remote_fingerprints(repositories)
    except OSError as exc:
        logger.warning("Could not check Cyphal DSDL updates (%s); using the local compile if present", exc)
        fingerprints = None

    stamp_matches = not force and fingerprints is not None and stamp.get("fingerprints") == fingerprints
    needs_download = force or not sources_ready or (fingerprints is not None and not stamp_matches)
    needs_compile = force or not compiled_ready or needs_download

    if fingerprints is None and not force:
        needs_download = not sources_ready
        needs_compile = not compiled_ready

    if needs_download:
        try:
            logger.warning("Downloading Cyphal DSDL repositories into %s", dsdl_directory)
            download_dsdl_repositories(
                repositories,
                dsdl_directory=dsdl_directory,
                force=force or not stamp_matches,
            )
        except OSError as exc:
            if compiled_ready and sources_ready:
                logger.warning("Cyphal DSDL download failed (%s); keeping the existing compile", exc)
                needs_compile = False
            else:
                raise RuntimeError(
                    "Cyphal DSDL definitions are not installed and could not be downloaded. "
                    "Check network access, then retry or run `cyphal install --force`."
                ) from exc

    if needs_compile:
        if not _sources_present(dsdl_directory, repositories):
            raise RuntimeError("Cyphal DSDL sources are missing after download. Run `cyphal install --force`.")
        _compile_namespaces(dsdl_directory, output_directory, repositories)
        if fingerprints is None:
            try:
                fingerprints = _remote_fingerprints(repositories)
            except OSError:
                fingerprints = None
        if fingerprints is not None:
            _write_stamp(output_directory, fingerprints)

    update_cyphal_path(dsdl_directory)
    os.environ["PYCYPHAL_PATH"] = str(output_directory)
    _prepend_sys_path(output_directory)
    return output_directory


def update_cyphal_path(dsdl_directory: Path) -> None:
    cyphal_path_str = os.environ.get("CYPHAL_PATH", "").replace(os.pathsep, ";").split(";")
    cyphal_path = [d for d in cyphal_path_str if d.strip()]  # filter out empty strings
    if str(dsdl_directory) not in cyphal_path:
        cyphal_path.append(str(dsdl_directory))
        logger.info("Adding %s to CYPHAL_PATH", dsdl_directory)
        os.environ["CYPHAL_PATH"] = os.pathsep.join(cyphal_path)


if not dsdl_updates_disabled():
    logger.debug("Downloading DSDL repositories.")
    _dsdl_directory = get_default_dsdl_dir()
    download_dsdl_repositories(dsdl_directory=_dsdl_directory)
    update_cyphal_path(_dsdl_directory)
else:
    logger.debug("DSDL repositories download skipped.")
