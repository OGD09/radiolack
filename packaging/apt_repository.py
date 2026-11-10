#!/usr/bin/env python3
"""Build and sign Radiolack's APT repository.

The repository holds Radiolack's Debian packages in the layout that apt
reads, for the suite "stable" and the component "main", with
architecture-independent packages only:

    pool/main/r/radiolack/radiolack_VERSION_all.deb
    dists/stable/main/binary-all/Packages, Packages.gz and by-hash/SHA256/
    dists/stable/Release, InRelease and Release.gpg

    python3 packaging/apt_repository.py --key FINGERPRINT REPOSITORY [PACKAGE.deb ...]

adds the packages given to the repository's pool, writes the index of every
package in the pool and the Release file that lists the index's checksums,
and signs the Release file with the GnuPG key FINGERPRINT, as InRelease and
Release.gpg. GnuPG asks for the key's passphrase as it always does, and signs
with the key's signing subkey. Without packages, it signs the repository
again, as it must before the signing subkey expires: apt refuses a signature
by an expired key.

The key signs only what it signed before and the packages given: apart from
those, the pool must hold exactly the packages of the index that the
repository's InRelease lists, signed by the same key. A published version
never changes. Old indexes stay under their checksum (Acquire-By-Hash), so
that apt never pairs a new Release file with an index that a web cache still
holds from before. The repository changes only once every check and both
signatures succeed. Needs Python, dpkg-deb, gpg and gpgv.
"""

import argparse
import email.utils
import gzip
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

PACKAGE = "radiolack"
SUITE = "stable"
COMPONENT = "main"
ARCHITECTURE = "all"
DISTRIBUTION = Path("dists") / SUITE
INDEX = Path(COMPONENT) / f"binary-{ARCHITECTURE}" / "Packages"     # below DISTRIBUTION
FINGERPRINT = re.compile(r"[0-9A-F]{40}|[0-9A-F]{64}", re.IGNORECASE)
SHA256 = re.compile(r"[0-9a-f]{64}")
# Fields that only the index may give: apt would take them from a package too.
INDEX_FIELDS = frozenset({"filename", "size", "md5sum", "sha1", "sha256", "sha512"})


@dataclass(frozen=True)
class Package:
    """A Debian package of Radiolack: its control file, its bytes and their
    checksum."""
    version: str
    control: str
    data: bytes = field(repr=False)
    sha256: str

    @property
    def listing(self) -> tuple[int, str]:
        """The package's size and checksum, as the index lists them."""
        return len(self.data), self.sha256

    @property
    def pool_path(self) -> str:
        """Where the package lives in the repository, as Debian lays out its
        pools; the file name leaves out the version's epoch."""
        upstream = self.version.split(":", 1)[-1]
        return f"pool/{COMPONENT}/{PACKAGE[0]}/{PACKAGE}/{PACKAGE}_{upstream}_{ARCHITECTURE}.deb"


def control_fields(paragraph: str) -> dict[str, str]:
    """The fields of one control paragraph, by lower-case name. Raises
    ValueError for anything else, such as an empty line."""
    fields: dict[str, str] = {}
    name = None
    for line in paragraph.rstrip("\n").split("\n"):
        if line[:1] in (" ", "\t") and name is not None:
            fields[name] += "\n" + line
            continue
        match = re.fullmatch(r"([!-9;-~]+):[ \t]*(.*)", line)
        if match is None:
            raise ValueError(f"not a control field: {line[:80]!r}")
        name = match.group(1).lower()
        if name in fields:
            raise ValueError(f"the field {match.group(1)} appears twice")
        fields[name] = match.group(2)
    return fields


def read_package(path: Path) -> Package:
    """Radiolack's package in this file. Raises ValueError for a file that
    is not an architecture-independent Debian package of Radiolack that the
    index can describe, and RuntimeError when dpkg-deb is missing."""
    data = path.read_bytes()
    result = subprocess.run([_program("dpkg-deb", "run on Debian or Ubuntu"), "--info", str(path), "control"],
                            capture_output=True, encoding="utf-8", errors="replace", timeout=60)
    if result.returncode != 0:
        raise ValueError(f"{path} is not a Debian package: {result.stderr.strip()}")
    control = result.stdout.rstrip("\n") + "\n"
    fields = control_fields(control)
    if fields.get("package") != PACKAGE:
        raise ValueError(f"{path} holds {fields.get('package', 'no package')!r}, not {PACKAGE}")
    version = fields.get("version", "")
    if not re.fullmatch(r"([0-9]+:)?[0-9][0-9A-Za-z.+~-]*", version):
        raise ValueError(f"{path} has no valid version: {version[:80]!r}")
    if fields.get("architecture") != ARCHITECTURE:
        raise ValueError(f"{path} is for {fields.get('architecture')!r}: the repository holds "
                         f"{ARCHITECTURE!r} packages only")
    if INDEX_FIELDS & fields.keys():
        raise ValueError(f"{path} has fields that only the index may give: {sorted(INDEX_FIELDS & fields.keys())}")
    return Package(version, control, data, hashlib.sha256(data).hexdigest())


def read_pool(repository: Path) -> dict[str, Package]:
    """The packages in the repository's pool, by their path in the
    repository. Raises ValueError for a file that is not where the pool
    keeps it."""
    pool = repository / "pool"
    packages = {}
    for path in sorted(pool.rglob("*")) if pool.is_dir() else ():
        if path.is_dir():
            continue
        name = path.relative_to(repository).as_posix()
        package = read_package(path) if path.suffix == ".deb" else None
        if package is None or name != package.pool_path:
            raise ValueError(f"{name} does not belong in the pool")
        packages[name] = package
    return packages


def refuse_links(repository: Path) -> None:
    """Raise ValueError for a symbolic link where the repository's files
    go, which would send what this tool writes out of the repository."""
    for top in (repository / "pool", repository / "dists"):
        if top.is_symlink():
            raise ValueError(f"{top} is a symbolic link")
        for folder, folders, files in os.walk(top):
            for name in folders + files:
                if os.path.islink(os.path.join(folder, name)):
                    raise ValueError(f"{os.path.join(folder, name)} is a symbolic link")


def public_key(key: str) -> bytes:
    """The public part of the signing key, as a keyring that gpgv reads.
    Raises RuntimeError when gpg is missing or has no such key."""
    result = subprocess.run([_program("gpg", "install gnupg"), "--batch", "--export", key],
                            stdin=subprocess.DEVNULL, capture_output=True, timeout=60)
    if result.returncode != 0 or not result.stdout:
        raise RuntimeError(f"GnuPG has no key {key}")
    return result.stdout


def verified(signature: bytes, keyring: bytes, data: bytes | None = None) -> bytes:
    """What the signature covers, once gpgv finds that a key of the keyring
    made it: the text of a clearsigned message, without anything around it,
    or the data of a detached signature. Raises ValueError for a signature
    that does not check out, and RuntimeError when gpgv is missing."""
    gpgv = _program("gpgv", "install gpgv")
    with tempfile.TemporaryDirectory() as temporary:
        folder = Path(temporary)
        (folder / "keyring.gpg").write_bytes(keyring)
        (folder / "signature").write_bytes(signature)
        if data is None:
            arguments = ["--output", str(folder / "text"), str(folder / "signature")]
        else:
            (folder / "data").write_bytes(data)
            arguments = [str(folder / "signature"), str(folder / "data")]
        result = subprocess.run([gpgv, "--keyring", str(folder / "keyring.gpg"), *arguments],
                                stdin=subprocess.DEVNULL, capture_output=True, encoding="utf-8",
                                errors="replace", timeout=60)
        if result.returncode != 0:
            raise ValueError(f"the signature does not check out: {result.stderr.strip() or result.returncode}")
        return (folder / "text").read_bytes() if data is None else data


def signed_index(repository: Path, keyring: bytes) -> dict[str, tuple[int, str]]:
    """The size and checksum of each package that the repository's signed
    index lists, by its path in the repository, once its InRelease checks
    out with the keyring and the index with InRelease; nothing for a
    repository without InRelease. Raises ValueError otherwise."""
    in_release = repository / DISTRIBUTION / "InRelease"
    if not in_release.exists():
        return {}
    try:
        release = control_fields(verified(in_release.read_bytes(), keyring).decode("utf-8"))
    except ValueError as error:
        raise ValueError(f"{in_release}: {error}") from None
    listed = {}
    for line in release.get("sha256", "").split("\n"):
        parts = line.split()
        if len(parts) == 3:
            listed[parts[2]] = (parts[0], parts[1])
    digest, size = listed.get(INDEX.as_posix(), ("", ""))
    if not SHA256.fullmatch(digest) or not size.isdigit():
        raise ValueError(f"{in_release} lists no checksum for {INDEX}")
    path = repository / DISTRIBUTION / INDEX.parent / "by-hash" / "SHA256" / digest
    index = path.read_bytes() if path.is_file() else b""
    if len(index) != int(size) or hashlib.sha256(index).hexdigest() != digest:
        raise ValueError(f"{path} is missing, or is not the index that {in_release} lists")
    packages = {}
    for paragraph in index.decode("utf-8").split("\n\n"):
        if paragraph.strip():
            fields = control_fields(paragraph)
            if not fields.get("size", "").isdigit() or not SHA256.fullmatch(fields.get("sha256", "")):
                raise ValueError(f"{path} lists {fields.get('filename')} without its size or checksum")
            packages[fields.get("filename", "")] = (int(fields["size"]), fields["sha256"])
    return packages


def packages_index(packages: dict[str, Package]) -> str:
    """The Packages index: each package's control file, followed by its
    place in the pool, its size and its checksum, in the order of the
    pool."""
    return "\n".join(f"{package.control}Filename: {name}\nSize: {len(package.data)}\nSHA256: {package.sha256}\n"
                     for name, package in sorted(packages.items()))


def release_file(indexes: dict[Path, bytes], date: datetime) -> str:
    """The suite's Release file, dated ``date``, with the size and checksum
    of each index, by its path below dists/SUITE."""
    lines = [
        "Origin: Radiolack",
        "Label: Radiolack",
        f"Suite: {SUITE}",
        f"Codename: {SUITE}",
        f"Date: {email.utils.format_datetime(date.astimezone(timezone.utc))}",
        f"Architectures: {ARCHITECTURE}",
        f"Components: {COMPONENT}",
        "Description: Radiolack, a receive-only SDR receiver",
        "Acquire-By-Hash: yes",
        "SHA256:",
    ]
    lines += [f" {hashlib.sha256(data).hexdigest()} {len(data)} {name.as_posix()}"
              for name, data in sorted(indexes.items())]
    return "\n".join(lines) + "\n"


def sign(release: bytes, key: str) -> tuple[bytes, bytes]:
    """InRelease and Release.gpg: this Release file signed with ``key``,
    clearsigned and detached. Raises RuntimeError when gpg is missing or
    cannot sign."""
    gpg = _program("gpg", "install gnupg")
    environment = dict(os.environ)
    if "GPG_TTY" not in environment and sys.stdin.isatty():
        environment["GPG_TTY"] = os.ttyname(sys.stdin.fileno())     # for a passphrase asked in the terminal
    signatures = []
    with tempfile.TemporaryDirectory() as temporary:
        folder = Path(temporary)
        (folder / "Release").write_bytes(release)
        for options in (["--clearsign"], ["--armor", "--detach-sign"]):
            output = folder / f"signature{len(signatures)}"
            result = subprocess.run([gpg, "--batch", "--quiet", "--local-user", key, "--digest-algo", "SHA512",
                                     *options, "--output", str(output), str(folder / "Release")],
                                    stdin=subprocess.DEVNULL, capture_output=True, encoding="utf-8",
                                    errors="replace", env=environment, timeout=300)
            if result.returncode != 0:
                raise RuntimeError(f"gpg could not sign with {key}: {result.stderr.strip() or result.returncode}")
            signatures.append(output.read_bytes())
    return signatures[0], signatures[1]


def update(repository: Path, key: str, additions: list[Path], date: datetime) -> tuple[dict[str, Package], set[str]]:
    """Add these packages to the repository's pool, write its index and its
    Release file, dated ``date``, and sign them with ``key``. Returns every
    package of the repository, by its path, and the paths of those added.
    Raises ValueError, RuntimeError or OSError, leaving the repository as it
    was."""
    if not FINGERPRINT.fullmatch(key):
        raise ValueError(f"name the signing key by its whole fingerprint, not {key[:80]!r}")
    keyring = public_key(key)
    refuse_links(repository)
    published = signed_index(repository, keyring)
    given: dict[str, Package] = {}
    for path in additions:
        package = read_package(path)
        name = package.pool_path
        known = published.get(name) or (given[name].listing if name in given else None)
        if known is not None and known != package.listing:
            raise ValueError(f"{path} differs from {PACKAGE} {package.version} in the repository: "
                             "a published version never changes")
        given[name] = package
    pool = read_pool(repository)
    missing = published.keys() - pool.keys()
    if missing:
        raise ValueError(f"{min(missing)} is missing from the pool, which the signed index lists")
    for name, package in pool.items():
        expected = published.get(name) or (given[name].listing if name in given else None)
        if expected is None:
            raise ValueError(f"{name} is in no signed index: remove it, or give the package again")
        if expected != package.listing:
            raise ValueError(f"{name} is not the package that the signed index lists")
    packages = {**pool, **given}
    if not packages:
        raise ValueError("the repository has no package yet: give the packages to add")
    text = packages_index(packages).encode("utf-8")
    indexes = {INDEX: text, INDEX.with_name("Packages.gz"): gzip.compress(text, compresslevel=9, mtime=0)}
    release = release_file(indexes, date).encode("utf-8")
    in_release, detached = sign(release, key)
    # What gpg wrote: signatures by the key, of this Release file.
    if verified(in_release, keyring) != release or verified(detached, keyring, release) != release:
        raise RuntimeError("gpg signed something else than the Release file")
    # Every check and both signatures passed: write, the packages first and InRelease last.
    added = given.keys() - pool.keys()
    for name in sorted(added):
        _write(repository / name, given[name].data)
    distribution = repository / DISTRIBUTION
    for name, data in indexes.items():
        _write(distribution / name.parent / "by-hash" / "SHA256" / hashlib.sha256(data).hexdigest(), data)
    for name, data in indexes.items():
        _write(distribution / name, data)
    _write(distribution / "Release", release)
    _write(distribution / "Release.gpg", detached)
    _write(distribution / "InRelease", in_release)
    return packages, added


def _program(name: str, advice: str) -> str:
    """The path of a program. Raises RuntimeError when it is missing."""
    path = shutil.which(name)
    if path is None:
        raise RuntimeError(f"{name} is missing: {advice}")
    return path


def _write(path: Path, data: bytes) -> None:
    """Replace a file at once, readable by everyone."""
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(f".{path.name}.partial")
    partial.write_bytes(data)
    partial.chmod(0o644)
    os.replace(partial, path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Add Debian packages of Radiolack to its APT repository, "
                                                 "then write the repository's index and sign it.")
    parser.add_argument("repository", type=Path, help="the repository's folder, made if missing")
    parser.add_argument("packages", type=Path, nargs="*", help="packages to add to the repository's pool")
    parser.add_argument("--key", required=True, help="fingerprint of the GnuPG key that signs the repository")
    args = parser.parse_args(argv)
    try:
        packages, added = update(args.repository, args.key, args.packages, datetime.now(timezone.utc))
    except (RuntimeError, ValueError, OSError, subprocess.SubprocessError) as error:     # such as a timeout
        print(f"apt_repository: {error}", file=sys.stderr)
        return 1
    for name, package in sorted(packages.items()):
        print(f"{'added' if name in added else 'kept'} {PACKAGE} {package.version}: {name}")
    print(f"signed {args.repository / DISTRIBUTION / 'InRelease'} with {args.key}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
