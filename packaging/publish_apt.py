#!/usr/bin/env python3
"""Publish a release of Radiolack in its APT repository, which GitHub Pages
serves from this repository's gh-pages branch.

    python3 packaging/publish_apt.py 0.3.0

The owner runs it once the release's workflow has published the release on
GitHub, on a computer that holds the repository's signing subkey. It:

1. fetches the gh-pages branch, checks its repository's signature with the
   key that the package installs, radiolack-archive-keyring.gpg, and stops
   there when the repository already has the release;
2. downloads the release's package and SHA256SUMS, and checks one against the
   other;
3. builds the package again from the release's tag, here, and checks that
   both packages are identical, byte for byte: the key signs only what the
   tagged source makes;
4. adds the package to the repository with apt_repository.py, which signs
   it (GnuPG asks for the key's passphrase), and checks the new signatures
   with radiolack-archive-keyring.gpg, as apt will;
5. commits the change to gh-pages, shows it, and pushes it once the owner
   agrees; GitHub Pages then publishes it. Nothing reaches GitHub before.

apt refuses a signature by an expired key: the command warns once the
signing subkey has less than a year left. Extend its expiry, put the new
public key in packaging/ and publish a release with it, which installations
receive while the old expiry still holds (README.md, APT repository). Needs
Python, git, dpkg-deb, gpg and gpgv.
"""

import argparse
import hashlib
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import apt_repository     # beside this file

PACKAGE = apt_repository.PACKAGE
HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
KEYRING = HERE / f"{PACKAGE}-archive-keyring.gpg"     # the key that the package installs for apt
BRANCH = "gh-pages"
RELEASES = "https://github.com/OGD09/radiolack/releases/download"
NOTICE = timedelta(days=365)     # how long before the signing subkey expires the command warns
DOWNLOAD_LIMIT = 64 * 2**20


def git(*args: str, folder: Path | None = None) -> str:
    """The output of a git command that must succeed. Raises RuntimeError
    when it fails."""
    result = subprocess.run(["git", *args], cwd=folder, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                            timeout=600)
    if result.returncode != 0:
        raise RuntimeError(f"git {args[0]} failed: {result.stderr.strip() or result.returncode}")
    return result.stdout


def signing_key(keyring: Path, now: datetime) -> tuple[str, datetime | None]:
    """The fingerprint of the keyring's key, and when the last of its valid
    signing subkeys expires, or None when one never does. Raises ValueError
    for a keyring that does not hold one valid public key with a valid
    signing subkey, or that holds anything secret."""
    with tempfile.TemporaryDirectory() as home:     # gpg writes there, not in the owner's GnuPG home
        result = subprocess.run(["gpg", "--batch", "--with-colons", "--fixed-list-mode", "--show-keys", str(keyring)],
                                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60,
                                env={**os.environ, "GNUPGHOME": home})
    if result.returncode != 0:
        raise ValueError(f"{keyring} is not a keyring: {result.stderr.strip() or result.returncode}")
    records = [line.split(":") for line in result.stdout.splitlines()]
    kinds = [record[0] for record in records]
    if kinds.count("pub") != 1 or {"sec", "ssb"} & set(kinds):
        raise ValueError(f"{keyring} must hold one public key, and nothing secret")
    primary = kinds.index("pub")
    if records[primary][1] in ("e", "r"):
        raise ValueError(f"the key in {keyring} has expired or been revoked")
    fingerprint = next((record[9] for record in records[primary + 1:] if record[0] == "fpr"), "")
    if not apt_repository.FINGERPRINT.fullmatch(fingerprint):
        raise ValueError(f"{keyring} gives no fingerprint for its key")
    expiries = []
    for record in records:
        if record[0] != "sub" or "s" not in record[11] or record[1] in ("e", "r"):
            continue
        expiry = datetime.fromtimestamp(int(record[6]), timezone.utc) if record[6] else None
        if expiry is None or expiry > now:
            expiries.append(expiry)
    if not expiries:
        raise ValueError(f"the key in {keyring} has no signing subkey that is still valid")
    return fingerprint, None if None in expiries else max(expiries)


def download(url: str, path: Path) -> None:
    """Save what the address serves. Raises OSError when the download fails,
    and ValueError for a file of more than DOWNLOAD_LIMIT bytes."""
    request = urllib.request.Request(url, headers={"User-Agent": f"{PACKAGE}-publish-apt"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            data = response.read(DOWNLOAD_LIMIT + 1)
    except urllib.error.URLError as error:     # an HTTP error too
        raise OSError(f"could not download {url}: {error}") from None
    if len(data) > DOWNLOAD_LIMIT:
        raise ValueError(f"{url} serves more than {DOWNLOAD_LIMIT} bytes")
    path.write_bytes(data)


def checksum(sums: str, name: str, data: bytes) -> str:
    """The package's SHA-256, once SHA256SUMS gives the same for its name.
    Raises ValueError otherwise."""
    digest = hashlib.sha256(data).hexdigest()
    for line in sums.splitlines():
        match = re.fullmatch(r"([0-9a-f]{64}) [ *](.+)", line)
        if match is not None and match.group(2) == name:
            if match.group(1) != digest:
                raise ValueError(f"SHA256SUMS gives {name} the checksum {match.group(1)}, "
                                 f"but the downloaded package's is {digest}")
            return digest
    raise ValueError(f"SHA256SUMS does not list {name}")


def rebuild(tag: str, maintainer: str, folder: Path) -> Path:
    """The package that the tag builds here, as the release's workflow
    builds it: from a clean checkout of the tag, with the tag's own
    build_deb.py and the release's maintainer. Raises RuntimeError when the
    build fails."""
    source = folder / "source"
    git("-c", "advice.detachedHead=false", "clone", "--quiet", "--branch", tag, str(ROOT), str(source))
    # The workflow sets no SOURCE_DATE_EPOCH: the date is the tagged commit's.
    environment = {name: value for name, value in os.environ.items() if name != "SOURCE_DATE_EPOCH"}
    result = subprocess.run([sys.executable, str(source / "packaging" / "build_deb.py"), "--maintainer", maintainer,
                             "--output", str(folder / "dist")],
                            stdin=subprocess.DEVNULL, capture_output=True, text=True, env=environment, timeout=600)
    if result.returncode != 0 or not result.stdout.strip():
        raise RuntimeError(f"the build of {tag} failed: {result.stderr.strip() or result.returncode}")
    return Path(result.stdout.strip().splitlines()[-1])


def confirm(question: str) -> bool:
    """Whether the owner answers yes."""
    try:
        return input(question).strip().lower() in ("y", "yes")
    except EOFError:
        return False


def publish(version: str, workspace: Path) -> int:
    """Publish this release, working in ``workspace``; see the module's
    description. Returns the exit status."""
    now = datetime.now(timezone.utc)
    fingerprint, expiry = signing_key(KEYRING, now)
    if expiry is not None and expiry - now < NOTICE:
        print(f"publish_apt: warning: the repository's signing subkey expires on {expiry:%Y-%m-%d}, and apt then "
              "refuses the repository: well before that, extend its expiry and publish a release with the new "
              "public key (README.md, APT repository)", file=sys.stderr)
    tag = f"v{version}"
    try:
        git("rev-parse", "--verify", "--quiet", f"refs/tags/{tag}^{{commit}}", folder=ROOT)
    except RuntimeError:
        raise ValueError(f"there is no tag {tag} here: fetch the tags (git fetch --tags), "
                         "or check the version") from None

    # The repository as GitHub Pages serves it, which the shipped key must have signed.
    remote = git("remote", "get-url", "origin", folder=ROOT).strip()
    pages = workspace / "pages"
    if git("ls-remote", "--heads", remote, f"refs/heads/{BRANCH}").strip():
        git("clone", "--quiet", "--single-branch", "--branch", BRANCH, remote, str(pages))
    else:
        print(f"publish_apt: origin has no {BRANCH} branch yet: this publication starts it")
        git("init", "--quiet", "--initial-branch", BRANCH, str(pages))
    keyring = KEYRING.read_bytes()
    name = f"{PACKAGE}_{version}_all.deb"
    if any(Path(path).name == name for path in apt_repository.signed_index(pages, keyring)):
        print(f"{PACKAGE} {version} is already in the APT repository: nothing to do")
        return 0

    # The release's package, which must be the one that its tag builds here.
    release = workspace / "release"
    release.mkdir()
    download(f"{RELEASES}/{tag}/{name}", release / name)
    download(f"{RELEASES}/{tag}/SHA256SUMS", release / "SHA256SUMS")
    digest = checksum((release / "SHA256SUMS").read_text(encoding="utf-8", errors="replace"), name,
                      (release / name).read_bytes())
    package = apt_repository.read_package(release / name)
    if package.version != version:
        raise ValueError(f"the release's {name} holds version {package.version}, not {version}")
    maintainer = apt_repository.control_fields(package.control).get("maintainer", "")
    rebuilt = rebuild(tag, maintainer, workspace / "build").read_bytes()
    if rebuilt != package.data:
        dpkg = subprocess.run(["dpkg-deb", "--version"], capture_output=True, text=True, timeout=60).stdout
        raise ValueError(f"the release's package is not the one that {tag} builds here: SHA-256 {digest} on GitHub, "
                         f"{hashlib.sha256(rebuilt).hexdigest()} here, built by {(dpkg.splitlines() or ['?'])[0]} "
                         "Nothing was signed")
    print(f"{name}: the package that {tag} builds here, SHA-256 {digest}; signing it with {fingerprint}")

    apt_repository.update(pages, fingerprint, [release / name], now)
    distribution = pages / apt_repository.DISTRIBUTION
    signed = (distribution / "Release").read_bytes()
    try:
        checked = (apt_repository.verified((distribution / "InRelease").read_bytes(), keyring),
                   apt_repository.verified((distribution / "Release.gpg").read_bytes(), keyring, signed))
    except ValueError as error:
        raise ValueError(f"{KEYRING.name}, which apt reads, does not check the new signatures: {error}. "
                         "Nothing was pushed") from None
    if checked != (signed, signed):
        raise ValueError("the new signatures do not cover the Release file. Nothing was pushed")
    (pages / ".nojekyll").touch()     # GitHub Pages serves the files as they are
    git("add", "--all", folder=pages)
    git("commit", "--quiet", "--message", f"Publish {PACKAGE} {version}", folder=pages)
    print("\n" + git("show", "--stat", "--format=%s", "HEAD", folder=pages).rstrip("\n"))
    if not confirm(f"Push this commit to the {BRANCH} branch of {remote}, which GitHub Pages publishes? [y/N] "):
        print("publish_apt: nothing was pushed")
        return 1
    git("push", "--quiet", remote, f"HEAD:refs/heads/{BRANCH}", folder=pages)
    print(f"published {PACKAGE} {version}: GitHub Pages serves it within a few minutes")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Publish a release of Radiolack in its APT repository, "
                                                 "which GitHub Pages serves.")
    parser.add_argument("version", help="the release's version, such as 0.3.0")
    args = parser.parse_args(argv)
    version = args.version.removeprefix("v")
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
        parser.error(f"a release's version has three numbers, such as 0.3.0, not {args.version!r}")
    try:
        with tempfile.TemporaryDirectory(prefix=f"{PACKAGE}-publish-") as workspace:
            return publish(version, Path(workspace))
    except (RuntimeError, ValueError, OSError, subprocess.SubprocessError) as error:     # such as a timeout
        print(f"publish_apt: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
