#!/usr/bin/env python3
"""Build Radiolack's Debian package, for Debian 12 or later and Ubuntu 24.04
or later.

The package installs Radiolack for the system's Python 3 and its packaged
libraries (aiohttp, NumPy, PyGObject, GTK 3 and WebKitGTK), with the
``radiolack`` command, its manual page, a menu entry, an icon, and the
changelog and licence notice that Debian keeps in /usr/share/doc/radiolack.
It also adds Radiolack's APT repository, its source and its key, so that apt
keeps Radiolack up to date.
Building needs only Python and dpkg-deb, as found on Debian and Ubuntu:

    python3 packaging/build_deb.py --maintainer "Name <address>"

writes dist/radiolack_VERSION_all.deb. A development version, such as
0.2.0.dev0, carries the date and commit of the checkout's last commit, and
.dirty when the checkout has changes since, so that a build of a later
commit installs over an earlier one. The package's maintainer, "Name
<address>", comes from --maintainer, or from DEBFULLNAME and DEBEMAIL as
Debian's tools read them. It is required: dpkg warns about an installed
package without one every time it reads its database.

The build is reproducible: every file in the package carries the date of
the checkout's last commit, or SOURCE_DATE_EPOCH when it is set, as for
Debian's own packages, so that two builds of a commit with the same
maintainer and the same dpkg are identical, byte for byte, and anyone can
check a published package against its source.
"""

from __future__ import annotations

import argparse
import email.utils
import hashlib
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import zlib
from datetime import datetime, timezone
from pathlib import Path

PACKAGE = "radiolack"
HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SOURCE = ROOT / "src" / PACKAGE
DIST_PACKAGES = Path("usr/lib/python3/dist-packages")
MANUAL_PAGE = f"{PACKAGE}.1"     # in packaging/, with @VERSION@ and @DATE@ to fill in
KEYRING = f"{PACKAGE}-archive-keyring.gpg"     # in packaging/: the public key of the APT repository
# The APT repository's source and key, from packaging/, as configuration files
# (conffiles): removing the package keeps both, so that the source still checks
# out; purging it removes both.
CONFFILES = {
    f"etc/apt/sources.list.d/{PACKAGE}.sources": f"{PACKAGE}.sources",
    f"etc/apt/keyrings/{KEYRING}": KEYRING,
}
# Lintian flags every package that adds an APT source; this one adds its own.
LINTIAN_OVERRIDES = f"""\
# The package adds Radiolack's own APT repository, signed with Radiolack's own
# key, so that apt keeps Radiolack up to date.
{PACKAGE}: package-installs-apt-sources [etc/apt/sources.list.d/{PACKAGE}.sources]
"""

# What the package needs from the system, at the versions of Debian 12.
DEPENDS = (
    "python3 (>= 3.11)",
    "python3-aiohttp (>= 3.8)",
    "python3-numpy (>= 1:1.24)",
    "python3-gi",
    "gir1.2-gtk-3.0",
    "gir1.2-webkit2-4.1 | gir1.2-webkit2-4.0",
    "ca-certificates",      # for the APT repository that the package adds, served over HTTPS
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


def last_commit(root: Path = ROOT) -> tuple[int, str, bool] | None:
    """The time (seconds since the epoch) and short name of the checkout's
    last commit, and whether the checkout has changes since; None outside a
    git checkout."""
    def git(*args: str) -> str:
        return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=True,
                              timeout=30).stdout
    try:
        output = git("log", "-1", "--format=%ct %h").split()
        dirty = bool(git("status", "--porcelain").strip())
    except (OSError, subprocess.SubprocessError):
        return None
    if len(output) != 2 or not output[0].isdigit():
        return None
    return int(output[0]), output[1], dirty


def commit_build(root: Path = ROOT) -> str | None:
    """The date and short name of the last commit, such as
    git20261110084512.938a320, followed by .dirty when the checkout has
    changes since; None outside a git checkout."""
    commit = last_commit(root)
    if commit is None:
        return None
    timestamp, name, dirty = commit
    moment = datetime.fromtimestamp(timestamp, timezone.utc)
    return f"git{moment:%Y%m%d%H%M%S}.{name}" + (".dirty" if dirty else "")


def source_date_epoch(environ=os.environ, root: Path = ROOT) -> int:
    """The date every file of the package carries: SOURCE_DATE_EPOCH when it
    is set, else the time of the checkout's last commit, else now (outside a
    git checkout, where the build cannot be reproduced). Raises ValueError
    for a SOURCE_DATE_EPOCH that is not a whole number of seconds."""
    given = environ.get("SOURCE_DATE_EPOCH", "").strip()
    if given:
        # A tar header holds dates up to 8**11 - 1 seconds, in 2242.
        if not given.isdigit() or int(given) >= 8**11:
            raise ValueError(f"SOURCE_DATE_EPOCH must be a whole number of seconds before 2242, not {given[:40]!r}")
        return int(given)
    commit = last_commit(root)
    return commit[0] if commit is not None else int(time.time())


def changelog(version: str, maintainer: str, epoch: int) -> str:
    """The package's Debian changelog: one entry, for this version, dated
    ``epoch``. Debian keeps it as /usr/share/doc/PACKAGE/changelog.gz for a
    package without a separate Debian revision, such as this one."""
    moment = email.utils.format_datetime(datetime.fromtimestamp(epoch, timezone.utc))
    return (f"{PACKAGE} ({version}) unstable; urgency=medium\n\n"
            f"  * Radiolack {version}.\n\n"
            f" -- {maintainer_field(maintainer)}  {moment}\n")


def manual_page(version: str, epoch: int) -> str:
    """The command's manual page, packaging/radiolack.1 with its version and
    date filled in."""
    day = datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%d")
    page = (HERE / MANUAL_PAGE).read_text(encoding="utf-8")
    for placeholder in ("@VERSION@", "@DATE@"):
        if placeholder not in page:
            raise ValueError(f"{MANUAL_PAGE} lacks {placeholder}")
    return page.replace("@VERSION@", version).replace("@DATE@", day)


def _compress(text: str) -> bytes:
    """``text`` in UTF-8, compressed as ``gzip -9n`` compresses Debian's
    documentation: at the highest level, with neither a name nor a date in
    the header. The header is written here, so that the bytes depend on the
    text and zlib only, not on the Python that builds the package."""
    data = text.encode("utf-8")
    deflate = zlib.compressobj(9, zlib.DEFLATED, -zlib.MAX_WBITS)
    body = deflate.compress(data) + deflate.flush()
    # Magic number, deflate, no flags, no date, best compression, Unix.
    header = b"\x1f\x8b\x08\x00\x00\x00\x00\x00\x02\x03"
    return header + body + struct.pack("<II", zlib.crc32(data), len(data) % 2**32)


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


def stage(tree: Path, version: str, maintainer: str, epoch: int) -> None:
    """Lay out the package's files and control files in ``tree``, all of
    them dated ``epoch``."""
    shutil.copytree(SOURCE, tree / DIST_PACKAGES / PACKAGE,
                    ignore=shutil.ignore_patterns("__pycache__", "*.py[cod]", ".*"))
    _write(tree / "usr/bin" / PACKAGE, LAUNCHER, executable=True)
    for folder, name in (("usr/share/applications", f"{PACKAGE}.desktop"),
                         ("usr/share/icons/hicolor/scalable/apps", f"{PACKAGE}.svg"),
                         (f"usr/share/doc/{PACKAGE}", "copyright")):
        _write(tree / folder / name, (HERE / name).read_text(encoding="utf-8"))
    _write(tree / f"usr/share/doc/{PACKAGE}/changelog.gz", _compress(changelog(version, maintainer, epoch)))
    _write(tree / f"usr/share/man/man1/{MANUAL_PAGE}.gz", _compress(manual_page(version, epoch)))
    for path, name in CONFFILES.items():
        _write(tree / path, (HERE / name).read_bytes())
    _write(tree / f"usr/share/lintian/overrides/{PACKAGE}", LINTIAN_OVERRIDES)
    tree.chmod(0o755)
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
    # As Debian's tools write it: dpkg keeps the checksums of the conffiles apart.
    _write(debian / "md5sums", "".join(
        f"{hashlib.md5(path.read_bytes()).hexdigest()}  {path.relative_to(tree).as_posix()}\n" for path in files
        if path.relative_to(tree).as_posix() not in CONFFILES))
    _write(debian / "conffiles", "".join(f"/{path}\n" for path in sorted(CONFFILES)))
    _write(debian / "postinst", POSTINST, executable=True)
    _write(debian / "prerm", PRERM, executable=True)
    debian.chmod(0o755)
    for path in (tree, *tree.rglob("*")):
        os.utime(path, (epoch, epoch))


def build(output: Path, maintainer: str) -> Path:
    """Build the package into ``output`` and return its path. Raises
    ValueError for a maintainer that does not read "Name <address>", and
    RuntimeError when dpkg-deb is missing or fails."""
    maintainer = maintainer_field(maintainer)
    dpkg_deb = shutil.which("dpkg-deb")
    if dpkg_deb is None:
        raise RuntimeError("dpkg-deb is missing: build on Debian or Ubuntu")
    version = debian_version(python_version(), commit_build())
    epoch = source_date_epoch()
    output.mkdir(parents=True, exist_ok=True)
    package = output / f"{PACKAGE}_{version}_all.deb"
    with tempfile.TemporaryDirectory() as temporary:
        tree = Path(temporary) / "tree"
        stage(tree, version, maintainer, epoch)
        # xz rather than zstd: every dpkg of the supported releases reads it.
        # SOURCE_DATE_EPOCH dates the archive's own members.
        result = subprocess.run([dpkg_deb, "--root-owner-group", "-Zxz", "--build", str(tree), str(package)],
                                capture_output=True, text=True, timeout=300,
                                env={**os.environ, "SOURCE_DATE_EPOCH": str(epoch)})
    if result.returncode != 0:
        raise RuntimeError(f"dpkg-deb failed: {result.stderr.strip() or result.stdout.strip()}")
    return package


def _write(path: Path, content: str | bytes, executable: bool = False) -> None:
    """Write text in UTF-8, or bytes as they are."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")
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
