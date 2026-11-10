#!/usr/bin/env python3
"""Build Radiolack's Debian package, for Debian 12 or later and Ubuntu 24.04
or later.

The package installs Radiolack for the system's Python 3 and its packaged
libraries (aiohttp, NumPy, PyGObject, GTK 3 and WebKitGTK), with the
``radiolack`` command, a menu entry, an icon and the licence notice that
Debian keeps in /usr/share/doc/radiolack/copyright. Building needs only
Python and dpkg-deb, as found on Debian and Ubuntu:

    python3 packaging/build_deb.py --maintainer "Name <address>"

writes dist/radiolack_VERSION_all.deb. A development version, such as
0.2.0.dev0, carries the date and commit of the checkout's last commit, and
.dirty when the checkout has changes since, so that a build of a later
commit installs over an earlier one. The package's maintainer, "Name
<address>", comes from --maintainer, or from DEBFULLNAME and DEBEMAIL as
Debian's tools read them. It is required: dpkg warns about an installed
package without one every time it reads its database.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

PACKAGE = "radiolack"
HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SOURCE = ROOT / "src" / PACKAGE
DIST_PACKAGES = Path("usr/lib/python3/dist-packages")

# What the package needs from the system, at the versions of Debian 12.
DEPENDS = (
    "python3 (>= 3.11)",
    "python3-aiohttp (>= 3.8)",
    "python3-numpy (>= 1:1.24)",
    "python3-gi",
    "gir1.2-gtk-3.0",
    "gir1.2-webkit2-4.1 | gir1.2-webkit2-4.0",
)
RECOMMENDS = ("rtl-sdr",)      # rtl_tcp, for an RTL-SDR receiver plugged into this computer
DESCRIPTION = """\
Receive-only SDR receiver with band plans
 Radiolack shows the spectrum and the waterfall of an SDR receiver in a
 desktop window and plays NFM, AM, USB, LSB and CW. It reads an RTL-SDR
 receiver plugged into this computer, or one that rtl_tcp serves from
 another computer, and labels every frequency with a simplified European
 band plan, with the IARU Region 1 plans inside the amateur bands.
"""

LAUNCHER = """\
#!/usr/bin/python3
\"\"\"Start Radiolack, as installed by its Debian package.\"\"\"

import sys

from radiolack.cli import main

sys.exit(main())
"""

# Byte-compiled with the system's tools, as Debian's Python packages are.
POSTINST = f"""\
#!/bin/sh
set -e
if command -v py3compile >/dev/null 2>&1; then
    py3compile -p {PACKAGE}
fi
"""
PRERM = f"""\
#!/bin/sh
set -e
if command -v py3clean >/dev/null 2>&1; then
    py3clean -p {PACKAGE}
else
    find /{DIST_PACKAGES}/{PACKAGE} -depth -type d -name __pycache__ -exec rm -rf {{}} +
fi
"""


def python_version(init: Path = SOURCE / "__init__.py") -> str:
    """The version in the package's __init__.py."""
    match = re.search(r'^__version__ = "([^"]+)"$', init.read_text(encoding="utf-8"), re.MULTILINE)
    if match is None:
        raise ValueError(f"no __version__ in {init}")
    return match.group(1)


def debian_version(version: str, build: str | None = None) -> str:
    """The Debian version of a release (1.2.0) or a development version
    (1.2.0.dev0 becomes 1.2.0~dev0, before 1.2.0). ``build``, such as the
    commit, follows a development version."""
    match = re.fullmatch(r"(\d+(?:\.\d+)*)(?:\.dev(\d+))?", version)
    if match is None:
        raise ValueError(f"a version such as 1.2.0 or 1.2.0.dev0 is needed, not {version!r}")
    release, dev = match.groups()
    if dev is None:
        return release
    return f"{release}~dev{dev}" + (f"+{build}" if build else "")


def commit_build(root: Path = ROOT) -> str | None:
    """The date and short name of the last commit, such as
    git20261110084512.938a320, followed by .dirty when the checkout has
    changes since; None outside a git checkout."""
    def git(*args: str) -> str:
        return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=True,
                              timeout=30).stdout
    try:
        output = git("log", "-1", "--format=%ct %h").split()
        dirty = bool(git("status", "--porcelain").strip())
    except (OSError, subprocess.SubprocessError):
        return None
    if len(output) != 2:
        return None
    timestamp, commit = output
    moment = datetime.fromtimestamp(int(timestamp), timezone.utc)
    return f"git{moment:%Y%m%d%H%M%S}.{commit}" + (".dirty" if dirty else "")


def default_maintainer(environ=os.environ) -> str | None:
    """"Name <address>" from DEBFULLNAME and DEBEMAIL, when both are set."""
    name, address = environ.get("DEBFULLNAME", "").strip(), environ.get("DEBEMAIL", "").strip()
    return f"{name} <{address}>" if name and address else None


def maintainer_field(maintainer: str) -> str:
    """The maintainer as the control file has it: one line, "Name
    <address>". Raises ValueError otherwise."""
    maintainer = maintainer.strip()
    if not re.fullmatch(r"[^<>\r\n]+ <[^<>\s]+@[^<>\s]+>", maintainer):
        raise ValueError(f'the maintainer must read "Name <address>", not {maintainer[:80]!r}')
    return maintainer


def stage(tree: Path, version: str, maintainer: str) -> None:
    """Lay out the package's files and control files in ``tree``."""
    shutil.copytree(SOURCE, tree / DIST_PACKAGES / PACKAGE,
                    ignore=shutil.ignore_patterns("__pycache__", "*.py[cod]", ".*"))
    _write(tree / "usr/bin" / PACKAGE, LAUNCHER, executable=True)
    for folder, name in (("usr/share/applications", f"{PACKAGE}.desktop"),
                         ("usr/share/icons/hicolor/scalable/apps", f"{PACKAGE}.svg"),
                         (f"usr/share/doc/{PACKAGE}", "copyright")):
        _write(tree / folder / name, (HERE / name).read_text(encoding="utf-8"))
    for path in tree.rglob("*"):
        if path.is_dir():
            path.chmod(0o755)
        elif path.name != PACKAGE or path.parent.name != "bin":
            path.chmod(0o644)
    files = sorted(path for path in tree.rglob("*") if path.is_file())
    installed_kib = sum((path.stat().st_size + 1023) // 1024 for path in files) + len(
        [path for path in tree.rglob("*") if path.is_dir()])
    control = [
        f"Package: {PACKAGE}",
        f"Version: {version}",
        "Architecture: all",
        f"Maintainer: {maintainer_field(maintainer)}",
        f"Installed-Size: {installed_kib}",
        f"Depends: {', '.join(DEPENDS)}",
        f"Recommends: {', '.join(RECOMMENDS)}",
        "Section: hamradio",
        "Priority: optional",
        f"Description: {DESCRIPTION}",
    ]
    debian = tree / "DEBIAN"
    _write(debian / "control", "\n".join(control).rstrip("\n") + "\n")
    _write(debian / "md5sums", "".join(
        f"{hashlib.md5(path.read_bytes()).hexdigest()}  {path.relative_to(tree).as_posix()}\n" for path in files))
    _write(debian / "postinst", POSTINST, executable=True)
    _write(debian / "prerm", PRERM, executable=True)
    debian.chmod(0o755)


def build(output: Path, maintainer: str) -> Path:
    """Build the package into ``output`` and return its path. Raises
    ValueError for a maintainer that does not read "Name <address>", and
    RuntimeError when dpkg-deb is missing or fails."""
    maintainer = maintainer_field(maintainer)
    dpkg_deb = shutil.which("dpkg-deb")
    if dpkg_deb is None:
        raise RuntimeError("dpkg-deb is missing: build on Debian or Ubuntu")
    version = debian_version(python_version(), commit_build())
    output.mkdir(parents=True, exist_ok=True)
    package = output / f"{PACKAGE}_{version}_all.deb"
    with tempfile.TemporaryDirectory() as temporary:
        tree = Path(temporary) / "tree"
        stage(tree, version, maintainer)
        # xz rather than zstd: every dpkg of the supported releases reads it.
        result = subprocess.run([dpkg_deb, "--root-owner-group", "-Zxz", "--build", str(tree), str(package)],
                                capture_output=True, text=True, timeout=300)
    if result.returncode != 0:
        raise RuntimeError(f"dpkg-deb failed: {result.stderr.strip() or result.stdout.strip()}")
    return package


def _write(path: Path, text: str, executable: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755 if executable else 0o644)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build Radiolack's Debian package.")
    parser.add_argument("--output", type=Path, default=ROOT / "dist",
                        help="folder for the package (default: dist in the repository)")
    parser.add_argument("--maintainer", default=default_maintainer(),
                        help='"Name <address>" (default: from DEBFULLNAME and DEBEMAIL)')
    args = parser.parse_args(argv)
    if not args.maintainer:
        parser.error('a maintainer is needed: give --maintainer "Name <address>", or set DEBFULLNAME and DEBEMAIL')
    try:
        package = build(args.output, args.maintainer)
    except (RuntimeError, ValueError, OSError) as error:
        print(f"build_deb: {error}", file=sys.stderr)
        return 1
    print(package)
    return 0


if __name__ == "__main__":
    sys.exit(main())
