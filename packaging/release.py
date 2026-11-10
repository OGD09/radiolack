#!/usr/bin/env python3
"""Release a version of Radiolack: tag it on main, then start the next one.

Versions have three numbers, X.Y.Z, and each release is an annotated tag
vX.Y.Z on main. Between two releases, src/radiolack/__init__.py holds the
next version as a development version, such as 0.2.1.dev0 after 0.2.0, so
that a Debian package built in between installs over the release.

    python3 packaging/release.py 0.2.1

checks that the checkout is on main, without changes, at the commit of
origin's main, and that 0.2.1 comes neither before the package's version nor
before an existing release tag. It then commits the version 0.2.1
("chore: release 0.2.1"), tags that commit v0.2.1 and commits the next
development version ("chore: start 0.2.2.dev0"). It pushes nothing: it
prints the command that publishes the two commits and the tag together. On
GitHub, the tag starts the Release workflow (.github/workflows/release.yml),
which checks it and publishes the release's Debian package.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
INIT = Path("src") / "radiolack" / "__init__.py"
BRANCH = "main"
REMOTE = "origin"

_NUMBER = r"(0|[1-9][0-9]*)"
_RELEASE = re.compile(rf"{_NUMBER}\.{_NUMBER}\.{_NUMBER}")
_PACKAGE_VERSION = re.compile(rf"{_NUMBER}\.{_NUMBER}\.{_NUMBER}(?:\.dev{_NUMBER})?")
_VERSION_LINE = re.compile(r'^__version__ = "([^"]*)"$', re.MULTILINE)

Version = tuple[int, int, int]


class ReleaseError(Exception):
    """A check or a git command failed; the release stops there."""


def parse_release(text: str) -> Version:
    """A release's three numbers. Raises ValueError for anything else, such
    as 0.2, v0.2.1 or 0.2.1.dev0."""
    match = _RELEASE.fullmatch(text)
    if match is None:
        raise ValueError(f"a version with three numbers, such as 0.2.1, is needed, not {text!r}")
    major, minor, patch = (int(part) for part in match.groups())
    return major, minor, patch


def parse_package_version(text: str) -> Version:
    """The release that the package's version leads to: 0.2.1 for 0.2.1.dev0,
    as for 0.2.1. Raises ValueError for other versions."""
    match = _PACKAGE_VERSION.fullmatch(text)
    if match is None:
        raise ValueError(f"the package's version, {text!r}, is neither X.Y.Z nor X.Y.Z.devN")
    major, minor, patch = (int(part) for part in match.groups()[:3])
    return major, minor, patch


def version_text(version: Version) -> str:
    return ".".join(str(part) for part in version)


def next_development(release: Version) -> str:
    """The development version that follows a release: 0.2.2.dev0 after 0.2.1."""
    major, minor, patch = release
    return f"{major}.{minor}.{patch + 1}.dev0"


def read_version(init: Path) -> str:
    """The version in the package's __init__.py."""
    found = _VERSION_LINE.findall(init.read_text(encoding="utf-8"))
    if len(found) != 1:
        raise ReleaseError(f"{init} needs exactly one __version__ line")
    return found[0]


def write_version(init: Path, version: str) -> None:
    """Sets the version in the package's __init__.py, rewriting the file."""
    source = init.read_text(encoding="utf-8")
    updated, count = _VERSION_LINE.subn(f'__version__ = "{version}"', source)
    if count != 1:
        raise ReleaseError(f"{init} needs exactly one __version__ line")
    init.write_text(updated, encoding="utf-8")


def git(root: Path, *args: str) -> str:
    """Runs git in the checkout and returns what it prints. Raises
    ReleaseError with git's message when it fails."""
    try:
        result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as error:
        raise ReleaseError(f"git {args[0]}: {error}") from error
    if result.returncode != 0:
        message = (result.stderr or result.stdout).strip() or f"exit status {result.returncode}"
        raise ReleaseError(f"git {args[0]} failed: {message}")
    return result.stdout


def release_tags(root: Path) -> list[Version]:
    """The versions of the checkout's vX.Y.Z tags."""
    versions = []
    for name in git(root, "tag", "--list", "v*").split():
        if _RELEASE.fullmatch(name[1:]):
            versions.append(parse_release(name[1:]))
    return versions


def check(root: Path, release: Version) -> str:
    """Checks that the release can be made from the checkout, after fetching
    origin's main and tags, and returns the package's current version."""
    branch = git(root, "branch", "--show-current").strip()
    if branch != BRANCH:
        raise ReleaseError(f"releases are made on {BRANCH}, not on {branch or 'a detached HEAD'}")
    if git(root, "status", "--porcelain").strip():
        raise ReleaseError("the checkout has changes: commit or remove them first")
    git(root, "fetch", "--quiet", "--tags", REMOTE, BRANCH)
    if git(root, "rev-parse", "HEAD") != git(root, "rev-parse", f"refs/remotes/{REMOTE}/{BRANCH}"):
        raise ReleaseError(f"{BRANCH} is not at the commit of {REMOTE}/{BRANCH}: push or pull first")
    tag = f"v{version_text(release)}"
    if git(root, "tag", "--list", tag).strip():
        raise ReleaseError(f"the tag {tag} exists already")
    current = read_version(root / INIT)
    try:
        leads_to = parse_package_version(current)
    except ValueError as error:
        raise ReleaseError(str(error)) from error
    if release < leads_to:
        raise ReleaseError(f"{version_text(release)} comes before the package's version, {current}")
    later = [version for version in release_tags(root) if version >= release]
    if later:
        raise ReleaseError(f"{version_text(release)} does not come after the release v{version_text(max(later))}")
    return current


def make_release(root: Path, release: Version) -> tuple[str, str]:
    """Commits and tags the release, then commits the next development
    version. Returns the tag and that version."""
    current = check(root, release)
    number = version_text(release)
    tag = f"v{number}"
    following = next_development(release)
    init = root / INIT
    done: list[str] = []
    try:
        if current != number:
            write_version(init, number)
            git(root, "commit", "--quiet", "-m", f"chore: release {number}", "--", str(INIT))
            done.append(f"committed {number}")
        git(root, "tag", "--annotate", tag, "-m", f"Radiolack {number}")
        done.append(f"tagged {tag}")
        write_version(init, following)
        git(root, "commit", "--quiet", "-m", f"chore: start {following}", "--", str(INIT))
    except ReleaseError as error:
        raise ReleaseError(f"{error} (done before: {', '.join(done) or 'nothing'})") from error
    return tag, following


def main(argv: list[str] | None = None, root: Path | None = None) -> int:
    parser = argparse.ArgumentParser(description="Tag a release of Radiolack on main and start the next version.")
    parser.add_argument("version", help="the release, with three numbers, such as 0.2.1")
    args = parser.parse_args(argv)
    try:
        release = parse_release(args.version)
    except ValueError as error:
        parser.error(str(error))
    try:
        tag, following = make_release(root or ROOT, release)
    except ReleaseError as error:
        print(f"release: {error}", file=sys.stderr)
        return 1
    print(f"Tagged {tag}; {BRANCH} now holds {following}. Publish the two commits and the tag together with:")
    print(f"    git push --atomic {REMOTE} {BRANCH} {tag}")
    print(f"On GitHub, the Release workflow then checks {tag}, builds its Debian package, tests it and "
          "publishes it on the tag's release page.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
