#!/usr/bin/env python3
"""Release helper for bumping local package version in pyproject.toml and uv.lock.

E.g.: python scripts/release.py --target-version 0.6.12
"""

from __future__ import annotations

import argparse
import re
import sys
import tomllib
from datetime import date
from pathlib import Path


def parse_args() -> argparse.Namespace:
    """Parse CLI arguments for release version update."""
    parser = argparse.ArgumentParser(
        description=(
            "Update version in pyproject.toml and uv.lock. "
            "If target version is omitted, it will be requested interactively."
        )
    )
    parser.add_argument(
        "-t",
        "--target-version",
        help="Target version to release (for example: 0.6.12)",
    )
    parser.add_argument(
        "--pyproject",
        default="pyproject.toml",
        help="Path to pyproject.toml (default: %(default)s)",
    )
    parser.add_argument(
        "--uv-lock",
        default="uv.lock",
        help="Path to uv.lock (default: %(default)s)",
    )
    parser.add_argument(
        "--package-name",
        default="cyphal-device-library",
        help="Package name used in uv.lock package entry (default: %(default)s)",
    )
    parser.add_argument(
        "--changelog",
        default="CHANGELOG.md",
        help="Path to CHANGELOG.md in Keep a Changelog format (default: %(default)s)",
    )
    parser.add_argument(
        "--release-date",
        default=date.today().isoformat(),
        help="Release date for changelog entry in ISO format (default: today)",
    )
    return parser.parse_args()


def read_file(path: Path) -> str:
    """Read a UTF-8 text file."""
    if not path.exists():
        raise FileNotFoundError(f"File not found: {path}")
    return path.read_text(encoding="utf-8")


def write_file(path: Path, content: str) -> None:
    """Write UTF-8 text to file."""
    path.write_text(content, encoding="utf-8")


def get_current_version_from_pyproject(pyproject_path: Path) -> str:
    """Extract [project].version from pyproject.toml."""
    data = tomllib.loads(read_file(pyproject_path))
    project = data.get("project", {})
    version = project.get("version")
    if not isinstance(version, str) or not version.strip():
        raise ValueError(f"Could not find [project].version in {pyproject_path}")
    return version.strip()


def prompt_target_version(current_version: str) -> str:
    """Request the target version from user input."""
    value = input(f"Target version (current: {current_version}): ").strip()
    if not value:
        raise ValueError("Target version must not be empty")
    return value


def update_pyproject_versions(pyproject_text: str, target_version: str) -> tuple[str, bool]:
    """Update version fields in pyproject.toml."""
    pattern = re.compile(rf'(?ms)(^\[{re.escape("project")}\]\n.*?^\s*{re.escape("version")}\s*=\s*")([^"]+)(")')
    updated, count = pattern.subn(rf"\g<1>{target_version}\g<3>", pyproject_text, count=1)
    if count <= 0:
        raise ValueError("Could not update [project].version in pyproject.toml")

    return updated, count > 0


def update_uv_lock_version(uv_lock_text: str, package_name: str, target_version: str) -> str:
    """Update package version for a package entry in uv.lock."""
    pattern = re.compile(
        rf'(?ms)(\[\[package\]\]\n.*?^name\s*=\s*"{re.escape(package_name)}"\n^version\s*=\s*")([^"]+)(")'
    )
    updated, count = pattern.subn(rf"\g<1>{target_version}\g<3>", uv_lock_text, count=1)
    if count == 0:
        raise ValueError(f'Could not find package "{package_name}" version entry in uv.lock')
    return updated


def update_changelog_for_release(
    changelog_text: str,
    target_version: str,
    release_date: str,
) -> str:
    """Move [Unreleased] notes into a new Keep a Changelog release section."""
    if re.search(rf"(?m)^## \[{re.escape(target_version)}\]\s*-\s*", changelog_text):
        raise ValueError(f"Version {target_version} already exists in CHANGELOG.md")

    unreleased_pattern = re.compile(r"(?ms)^## \[Unreleased\]\n(?P<body>.*?)(?=^## \[|\Z)")
    match = unreleased_pattern.search(changelog_text)
    if match is None:
        raise ValueError("Could not find [Unreleased] section in CHANGELOG.md")

    unreleased_body = match.group("body").strip()
    if not unreleased_body:
        unreleased_body = "### Added\n- _No changes listed._"

    new_unreleased = "## [Unreleased]\n\n### Added\n- _Nothing yet._\n\n"
    new_release = f"## [{target_version}] - {release_date}\n\n{unreleased_body}\n\n"

    updated = changelog_text[: match.start()] + new_unreleased + new_release + changelog_text[match.end() :]
    updated = re.sub(r"\n{3,}", "\n\n", updated).rstrip() + "\n"
    return updated


def main() -> int:
    """Run release version bump flow."""
    args = parse_args()

    pyproject_path = Path(args.pyproject)
    uv_lock_path = Path(args.uv_lock)
    changelog_path = Path(args.changelog)

    try:
        current_version = get_current_version_from_pyproject(pyproject_path)
        target_version = args.target_version.strip() if args.target_version else prompt_target_version(current_version)

        if target_version == current_version:
            print(f"No change needed. Version is already {current_version}")
            return 0

        pyproject_text = read_file(pyproject_path)
        uv_lock_text = read_file(uv_lock_path)
        changelog_text = read_file(changelog_path)

        pyproject_updated, _ = update_pyproject_versions(pyproject_text, target_version)
        uv_lock_updated = update_uv_lock_version(uv_lock_text, args.package_name, target_version)
        changelog_updated = update_changelog_for_release(changelog_text, target_version, args.release_date)

        write_file(pyproject_path, pyproject_updated)
        write_file(uv_lock_path, uv_lock_updated)
        write_file(changelog_path, changelog_updated)

        print(f"Updated {pyproject_path}: {current_version} -> {target_version}")
        print(f"Updated {uv_lock_path}: {current_version} -> {target_version}")
        print(f"Updated {changelog_path}: added {target_version} entry for {args.release_date}")
        return 0
    except Exception as error:
        print(f"Release helper failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
