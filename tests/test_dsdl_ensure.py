"""Automatic DSDL download and compile into the shared pycyphal directory."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from urllib.error import URLError

import pytest

_PREVIOUS_DOWNLOAD_FLAG = os.environ.get("CYPHAL_DEVICE_LIBRARY_NO_DSDL_DOWNLOAD")
os.environ["CYPHAL_DEVICE_LIBRARY_NO_DSDL_DOWNLOAD"] = "1"

from cyphal_device_library.util.dsdl import (  # noqa: E402
    DSDLRepository,
    ensure_dsdl_compiled,
    get_shared_compiled_directory,
)


def _restore_env(name: str, previous: str | None) -> None:
    if previous is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = previous


@pytest.fixture(autouse=True)
def _restore_import_state(monkeypatch: pytest.MonkeyPatch):
    """Keep a stub DSDL compile from replacing the real ``starcopter`` package."""
    monkeypatch.setattr(sys, "path", list(sys.path))
    cyphal_path = os.environ.get("CYPHAL_PATH")
    pycyphal_path = os.environ.get("PYCYPHAL_PATH")
    yield
    _restore_env("CYPHAL_PATH", cyphal_path)
    _restore_env("PYCYPHAL_PATH", pycyphal_path)


@pytest.fixture(scope="module", autouse=True)
def _restore_download_flag():
    """Do not leave the download opt-out set for later test modules."""
    yield
    _restore_env("CYPHAL_DEVICE_LIBRARY_NO_DSDL_DOWNLOAD", _PREVIOUS_DOWNLOAD_FLAG)


def _repo() -> DSDLRepository:
    return DSDLRepository(zip_url="https://example.invalid/starcopter.zip", namespaces=["starcopter"])


def _write_compiled(output: Path, namespace: str = "starcopter") -> None:
    init = output / namespace / "__init__.py"
    init.parent.mkdir(parents=True, exist_ok=True)
    init.write_text("# compiled\n", encoding="utf-8")


def _write_sources(cache: Path, namespace: str = "starcopter") -> None:
    (cache / namespace).mkdir(parents=True, exist_ok=True)


def test_shared_directory_uses_cache_when_default_is_not_writable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("PYCYPHAL_PATH", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr("cyphal_device_library.util.dsdl._directory_is_writable", lambda _path: False)

    assert get_shared_compiled_directory() == tmp_path / ".cache" / "pycyphal"


def test_ensure_downloads_and_compiles_when_output_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cache = tmp_path / "cache"
    output = tmp_path / "compiled"
    repo = _repo()
    downloaded: list[bool] = []
    compiled: list[Path] = []

    def fake_download(repositories, dsdl_directory, force=False):
        downloaded.append(force)
        for item in repositories:
            for namespace in item.namespaces:
                (dsdl_directory / namespace).mkdir(parents=True, exist_ok=True)

    def fake_compile(namespaces, output_directory):
        compiled.append(Path(output_directory))
        for namespace in namespaces:
            init = Path(output_directory) / Path(namespace).name / "__init__.py"
            init.parent.mkdir(parents=True, exist_ok=True)
            init.write_text("# compiled\n", encoding="utf-8")

    monkeypatch.setattr("cyphal_device_library.util.dsdl._remote_fingerprints", lambda _repos: {repo.zip_url: '"new"'})
    monkeypatch.setattr("cyphal_device_library.util.dsdl.download_dsdl_repositories", fake_download)
    monkeypatch.setattr("pycyphal.dsdl.compile_all", fake_compile)

    result = ensure_dsdl_compiled(repositories=[repo], dsdl_directory=cache, output_directory=output)

    assert result == output.resolve()
    assert downloaded == [True]
    assert compiled == [output.resolve()]
    assert str(cache.resolve()) in os.environ["CYPHAL_PATH"].split(os.pathsep)
    assert str(output.resolve()) == sys.path[0]
    stamp = json.loads((output / ".cyphal-dsdl-stamp.json").read_text(encoding="utf-8"))
    assert stamp["fingerprints"] == {repo.zip_url: '"new"'}


def test_ensure_skips_network_compile_when_fingerprint_matches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cache = tmp_path / "cache"
    output = tmp_path / "compiled"
    repo = _repo()
    _write_sources(cache)
    _write_compiled(output)
    (output / ".cyphal-dsdl-stamp.json").write_text(
        json.dumps({"fingerprints": {repo.zip_url: '"same"'}}),
        encoding="utf-8",
    )
    monkeypatch.setattr("cyphal_device_library.util.dsdl._remote_fingerprints", lambda _repos: {repo.zip_url: '"same"'})
    monkeypatch.setattr(
        "cyphal_device_library.util.dsdl.download_dsdl_repositories",
        lambda *_args, **_kwargs: pytest.fail("download should be skipped"),
    )
    monkeypatch.setattr("pycyphal.dsdl.compile_all", lambda *_args, **_kwargs: pytest.fail("compile should be skipped"))

    result = ensure_dsdl_compiled(repositories=[repo], dsdl_directory=cache, output_directory=output)

    assert result == output.resolve()


def test_ensure_recompiles_and_drops_stale_types_when_fingerprint_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = tmp_path / "cache"
    output = tmp_path / "compiled"
    repo = _repo()
    _write_sources(cache)
    _write_compiled(output)
    stale = output / "starcopter" / "obsolete.py"
    stale.write_text("stale\n", encoding="utf-8")
    (output / ".cyphal-dsdl-stamp.json").write_text(
        json.dumps({"fingerprints": {repo.zip_url: '"old"'}}),
        encoding="utf-8",
    )

    def fake_download(repositories, dsdl_directory, force=False):
        assert force is True
        for item in repositories:
            for namespace in item.namespaces:
                (dsdl_directory / namespace).mkdir(parents=True, exist_ok=True)

    def fake_compile(namespaces, output_directory):
        for namespace in namespaces:
            init = Path(output_directory) / Path(namespace).name / "__init__.py"
            init.parent.mkdir(parents=True, exist_ok=True)
            init.write_text("# compiled\n", encoding="utf-8")

    monkeypatch.setattr("cyphal_device_library.util.dsdl._remote_fingerprints", lambda _repos: {repo.zip_url: '"new"'})
    monkeypatch.setattr("cyphal_device_library.util.dsdl.download_dsdl_repositories", fake_download)
    monkeypatch.setattr("pycyphal.dsdl.compile_all", fake_compile)

    ensure_dsdl_compiled(repositories=[repo], dsdl_directory=cache, output_directory=output)

    assert not stale.exists()
    assert (output / "starcopter" / "__init__.py").is_file()


def test_ensure_keeps_existing_compile_when_upstream_lookup_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = tmp_path / "cache"
    output = tmp_path / "compiled"
    repo = _repo()
    _write_sources(cache)
    _write_compiled(output)

    def fail_lookup(_repos):
        raise URLError("offline")

    monkeypatch.setattr("cyphal_device_library.util.dsdl._remote_fingerprints", fail_lookup)
    monkeypatch.setattr(
        "cyphal_device_library.util.dsdl.download_dsdl_repositories",
        lambda *_args, **_kwargs: pytest.fail("download should be skipped while offline"),
    )

    result = ensure_dsdl_compiled(repositories=[repo], dsdl_directory=cache, output_directory=output)

    assert result == output.resolve()


def test_install_updates_shared_tree_and_site_packages(monkeypatch: pytest.MonkeyPatch) -> None:
    from cyphal_device_library.cli import dsdl as dsdl_cli

    calls: list[tuple[str, bool]] = []
    monkeypatch.setattr(
        dsdl_cli,
        "ensure_dsdl_compiled",
        lambda force=False: calls.append(("ensure", force)),
    )
    monkeypatch.setattr(
        dsdl_cli,
        "download_and_compile_dsdl_repositories",
        lambda force=False: calls.append(("site", force)),
    )
    monkeypatch.setattr(dsdl_cli.typer, "echo", lambda *_args, **_kwargs: None)

    dsdl_cli.install(force=True)

    assert calls == [("ensure", True), ("site", True)]


def test_ensure_raises_when_offline_and_nothing_is_compiled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _repo()

    def fail_lookup(_repos):
        raise URLError("offline")

    monkeypatch.setattr("cyphal_device_library.util.dsdl._remote_fingerprints", fail_lookup)
    monkeypatch.setattr(
        "cyphal_device_library.util.dsdl.download_dsdl_repositories",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(URLError("offline")),
    )

    with pytest.raises(RuntimeError, match="DSDL"):
        ensure_dsdl_compiled(
            repositories=[repo],
            dsdl_directory=tmp_path / "cache",
            output_directory=tmp_path / "compiled",
        )
