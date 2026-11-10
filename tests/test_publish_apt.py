"""Tests for the publication of Radiolack's APT repository
(packaging/publish_apt.py), and for the repository's key that the package
installs. Publications run in temporary git repositories, with a stand-in for
GitHub's downloads, and sign with throwaway keys, as the repository's own
tests do (tests/test_apt_repository.py). They need git, dpkg-deb and GnuPG."""

import hashlib
import importlib.util
import os
import re
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
from unittest import mock

from tests.test_apt_repository import TOOLS, run, throwaway_gnupg

ROOT = Path(__file__).resolve().parents[1]
PACKAGING = ROOT / "packaging"
with mock.patch.object(sys, "path", [str(PACKAGING), *sys.path]):     # it imports apt_repository from beside it
    spec = importlib.util.spec_from_file_location("publish_apt", PACKAGING / "publish_apt.py")
    publish_apt = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(publish_apt)
apt_repository = publish_apt.apt_repository

VERSION = "9.8.7"
NAME = f"radiolack_{VERSION}_all.deb"
MAINTAINER = "Test <test@example.invalid>"


def checksums(data, name=NAME):
    """SHA256SUMS, as the release's workflow writes it, for one package."""
    return f"{hashlib.sha256(data).hexdigest()}  {name}\n".encode()


@unittest.skipUnless(shutil.which("gpg"), "gpg")
class KeyringTest(unittest.TestCase):
    """The key that the package installs for apt: the owner's, as README.md
    gives it."""

    def setUp(self):
        self.now = datetime.now(timezone.utc)
        self.fingerprint, self.expiry = publish_apt.signing_key(publish_apt.KEYRING, self.now)

    def test_is_the_key_that_the_readme_gives(self):
        groups = [self.fingerprint[start:start + 4] for start in range(0, len(self.fingerprint), 4)]
        spaced = " ".join(groups[:5]) + "  " + " ".join(groups[5:])     # as gpg shows it
        self.assertTrue(spaced in (ROOT / "README.md").read_text(encoding="utf-8"),
                        f"README.md does not give the key's fingerprint, {spaced}")

    def test_its_primary_key_only_certifies(self):
        # The primary key stays offline; a subkey signs.
        listing = run("gpg", "--batch", "--with-colons", "--show-keys", str(publish_apt.KEYRING)).stdout
        records = [line.split(":") for line in listing.splitlines()]
        primary = next(record for record in records if record[0] == "pub")
        self.assertEqual("".join(letter for letter in primary[11] if letter.islower()), "c")

    def test_its_signing_subkey_has_a_year_left(self):
        if self.expiry is not None:
            self.assertGreaterEqual(
                self.expiry - self.now, publish_apt.NOTICE,
                f"the APT repository's signing subkey expires on {self.expiry:%Y-%m-%d}, and apt then refuses the "
                "repository: extend its expiry and publish a release with the new public key (README.md, APT "
                "repository)")


@unittest.skipUnless(all(shutil.which(tool) for tool in ("git", *TOOLS)), "git, dpkg-deb, gpg, gpgv and gpgconf")
class PublishTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        throwaway_gnupg(cls)
        # git as the tests set it, whatever the settings of the computer.
        settings = cls.folder / "gitconfig"
        settings.touch()
        environment = mock.patch.dict(os.environ, {
            "GIT_CONFIG_GLOBAL": str(settings), "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
            "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.invalid"})
        environment.start()
        cls.addClassCleanup(environment.stop)
        # Radiolack's source, released as VERSION in a repository of its own.
        cls.source = cls.folder / "source"
        ignore = shutil.ignore_patterns("__pycache__", "*.py[cod]")
        shutil.copytree(ROOT / "src" / "radiolack", cls.source / "src" / "radiolack", ignore=ignore)
        shutil.copytree(PACKAGING, cls.source / "packaging", ignore=ignore)
        init = cls.source / "src" / "radiolack" / "__init__.py"
        init.write_text(re.sub(r'(?m)^__version__ = ".*"$', f'__version__ = "{VERSION}"', init.read_text()))
        run("git", "init", "--quiet", "--initial-branch", "main", str(cls.source))
        run("git", "-C", str(cls.source), "add", "--all")
        run("git", "-C", str(cls.source), "commit", "--quiet", "--message", f"Release {VERSION}")
        run("git", "-C", str(cls.source), "tag", "--annotate", "--message", f"Radiolack {VERSION}", f"v{VERSION}")
        # The release's package, as its workflow builds it, and one that its tag does not build.
        cls.package = cls.build("release")
        extra = cls.source / "src" / "radiolack" / "extra.py"
        extra.write_text("# Not in the release.\n")
        cls.other_package = cls.build("other")
        extra.unlink()

    @classmethod
    def build(cls, folder):
        output = run(sys.executable, str(cls.source / "packaging" / "build_deb.py"), "--maintainer", MAINTAINER,
                     "--output", str(cls.folder / folder)).stdout
        return Path(output.strip())

    def setUp(self):
        self.origin = Path(tempfile.mkdtemp(dir=self.folder)) / "origin.git"
        run("git", "init", "--quiet", "--bare", str(self.origin))
        run("git", "-C", str(self.source), "remote", "remove", "origin", check=False)
        run("git", "-C", str(self.source), "remote", "add", "origin", str(self.origin))
        data = self.package.read_bytes()
        self.assets = {NAME: data, "SHA256SUMS": checksums(data)}
        self.downloads = []
        self.questions = []
        self.answer = "y"
        for patch in (mock.patch.object(publish_apt, "ROOT", self.source),
                      mock.patch.object(publish_apt, "KEYRING", self.keyring),
                      mock.patch.object(publish_apt, "download", self.download),
                      mock.patch("builtins.input", self.ask)):
            patch.start()
            self.addCleanup(patch.stop)

    def download(self, url, path):
        """GitHub's downloads: the release's assets."""
        self.downloads.append(url)
        prefix = f"{publish_apt.RELEASES}/v{VERSION}/"
        name = url.removeprefix(prefix)
        if not url.startswith(prefix) or name not in self.assets:
            raise OSError(f"could not download {url}: HTTP Error 404: Not Found")
        path.write_bytes(self.assets[name])

    def ask(self, question):
        self.questions.append(question)
        return self.answer

    def publish(self, version=VERSION):
        """The command's exit status, output and errors."""
        output, errors = StringIO(), StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            status = publish_apt.main([version])
        return status, output.getvalue(), errors.getvalue()

    def head(self):
        """The commit of origin's gh-pages branch, or None without one."""
        result = run("git", "-C", str(self.origin), "rev-parse", "--verify", "--quiet", "refs/heads/gh-pages",
                     check=False)
        return result.stdout.strip() or None

    def pages(self):
        """Every file of origin's gh-pages branch, with its bytes."""
        checkout = Path(tempfile.mkdtemp(dir=self.folder)) / "pages"
        run("git", "clone", "--quiet", "--branch", "gh-pages", str(self.origin), str(checkout))
        return {path.relative_to(checkout).as_posix(): path.read_bytes() for path in sorted(checkout.rglob("*"))
                if path.is_file() and ".git" not in path.relative_to(checkout).parts}

    def test_publishes_the_release_that_its_tag_builds(self):
        status, output, errors = self.publish()
        self.assertEqual(status, 0, output + errors)
        content = self.pages()
        self.assertEqual(content[f"pool/main/r/radiolack/{NAME}"], self.package.read_bytes())
        self.assertEqual(content[".nojekyll"], b"")
        release = content["dists/stable/Release"]
        keyring = self.keyring.read_bytes()
        self.assertEqual(apt_repository.verified(content["dists/stable/InRelease"], keyring), release)
        self.assertEqual(apt_repository.verified(content["dists/stable/Release.gpg"], keyring, release), release)
        self.assertEqual(run("git", "-C", str(self.origin), "log", "--format=%s", "gh-pages").stdout,
                         f"Publish radiolack {VERSION}\n")
        self.assertEqual(len(self.questions), 1)
        self.assertIn("signing subkey expires on", errors)     # the test key's, within a day
        # Once published, the release stays as it is: nothing to download, sign or push.
        head, self.downloads = self.head(), []
        status, output, errors = self.publish(f"v{VERSION}")
        self.assertEqual(status, 0, output + errors)
        self.assertIn(f"radiolack {VERSION} is already in the APT repository", output)
        self.assertEqual((self.downloads, self.head(), len(self.questions)), ([], head, 1))

    def test_pushes_nothing_without_the_owners_agreement(self):
        self.answer = "n"
        status, output, errors = self.publish()
        self.assertEqual(status, 1, output + errors)
        self.assertIn("nothing was pushed", output)
        self.assertIsNone(self.head())

    def test_refuses_a_release_that_its_tag_does_not_build(self):
        data = self.other_package.read_bytes()
        self.assets = {NAME: data, "SHA256SUMS": checksums(data)}
        asked = self.pinentry_log.read_text().count("asked") if self.pinentry_log.exists() else 0
        status, output, errors = self.publish()
        self.assertEqual(status, 1, output + errors)
        self.assertIn(f"the release's package is not the one that v{VERSION} builds here", errors)
        self.assertIn("Nothing was signed", errors)
        self.assertEqual((self.head(), self.questions), (None, []))
        self.assertEqual(self.pinentry_log.read_text().count("asked") if self.pinentry_log.exists() else 0, asked)

    def test_refuses_a_package_that_its_checksums_do_not_list(self):
        data = self.package.read_bytes()
        for sums, message in ((checksums(b"other"), f"SHA256SUMS gives {NAME} the checksum"),
                              (checksums(data, "other.deb"), f"SHA256SUMS does not list {NAME}")):
            with self.subTest(message=message):
                self.assets["SHA256SUMS"] = sums
                status, output, errors = self.publish()
                self.assertEqual(status, 1, output + errors)
                self.assertIn(message, errors)
                self.assertIsNone(self.head())

    def test_needs_the_releases_tag_and_its_assets(self):
        status, output, errors = self.publish("9.9.9")
        self.assertEqual(status, 1, output + errors)
        self.assertIn("there is no tag v9.9.9 here", errors)
        self.assets = {}
        status, output, errors = self.publish()
        self.assertEqual(status, 1, output + errors)
        self.assertIn(f"could not download {publish_apt.RELEASES}/v{VERSION}/{NAME}", errors)
        self.assertIsNone(self.head())

    def test_adds_only_to_a_repository_that_the_key_signed(self):
        pages = Path(tempfile.mkdtemp(dir=self.folder)) / "pages"
        run("git", "init", "--quiet", "--initial-branch", "gh-pages", str(pages))
        apt_repository.update(pages, self.other_key, [self.package], datetime.now(timezone.utc))
        run("git", "-C", str(pages), "add", "--all")
        run("git", "-C", str(pages), "commit", "--quiet", "--message", "Another key's repository")
        run("git", "-C", str(pages), "push", "--quiet", str(self.origin), "HEAD:refs/heads/gh-pages")
        head = self.head()
        status, output, errors = self.publish()
        self.assertEqual(status, 1, output + errors)
        self.assertIn("the signature does not check out", errors)
        self.assertEqual((self.downloads, self.head()), ([], head))

    def test_publishes_releases_only(self):
        for version in ("0.3", "0.3.0.dev0", "latest"):
            with self.subTest(version=version), redirect_stderr(StringIO()), self.assertRaises(SystemExit):
                publish_apt.main([version])

    def test_reads_one_public_key_with_a_valid_signing_subkey(self):
        now = datetime.now(timezone.utc)
        fingerprint, expiry = publish_apt.signing_key(self.keyring, now)
        self.assertEqual(fingerprint, self.key)
        self.assertLess(expiry - now, timedelta(days=2))
        secret = self.folder / "secret.gpg"
        secret.write_bytes(run("gpg", "--batch", "--export-secret-keys", self.other_key, text=False).stdout)
        both = self.folder / "both.gpg"
        both.write_bytes(self.keyring.read_bytes() + self.other_keyring.read_bytes())
        text = self.folder / "notes.gpg"
        text.write_text("not a key\n")
        for path, moment, message in ((self.keyring, now + timedelta(days=2), "no signing subkey that is still valid"),
                                      (secret, now, "nothing secret"), (both, now, "must hold one public key"),
                                      (text, now, "is not a keyring")):
            with self.subTest(message=message), self.assertRaises(ValueError) as caught:
                publish_apt.signing_key(path, moment)
            self.assertIn(message, str(caught.exception))


if __name__ == "__main__":
    unittest.main()
