"""Tests for Radiolack's APT repository (packaging/apt_repository.py): the
repositories it builds and signs, which apt reads, and those it refuses to
sign. They sign with throwaway keys in a GnuPG home of their own, whose
passphrase a stand-in pinentry gives as the owner types it, and run apt-get
as the current user in a folder of its own, away from the system's apt.
They need dpkg-deb and GnuPG (gpg, gpgv and gpgconf), and apt-get to read the
repositories, as on Debian and Ubuntu. On Debian 12, each apt-get run leaves
an empty folder in /run/user/UID/gnupg, which apt-key makes and the end of
the session removes."""

import gzip
import hashlib
import importlib.util
import os
import pwd
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("apt_repository", ROOT / "packaging" / "apt_repository.py")
apt_repository = importlib.util.module_from_spec(spec)
spec.loader.exec_module(apt_repository)

DATE = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)
PASSPHRASE = "throwaway passphrase"
POOL = "pool/main/r/radiolack"
INDEXES = "dists/stable/main/binary-all"
# Answers gpg-agent as a pinentry does, with the passphrase, and notes each request.
PINENTRY = """#!{python}
import sys
print("OK", flush=True)
for line in sys.stdin:
    if line.startswith("GETPIN"):
        with open({log!r}, "a") as log:
            log.write("asked\\n")
        print("D {passphrase}", flush=True)
    print("OK", flush=True)
    if line.startswith("BYE"):
        break
"""
TOOLS = ("dpkg-deb", "gpg", "gpgv", "gpgconf")
APT = shutil.which("apt-get") and os.path.exists("/usr/lib/apt/methods/file")


def run(*command, check=True, text=True, **options):
    """A program's run, with its output, as text unless asked otherwise; by
    default, one that succeeds."""
    result = subprocess.run(command, capture_output=True, text=text, timeout=120, **options)
    if check and result.returncode != 0:
        raise AssertionError(f"{command[0]} failed ({result.returncode}): {result.stdout}{result.stderr}")
    return result


def files(folder):
    """Every file below the folder, by its path in the folder, with its bytes."""
    return {path.relative_to(folder).as_posix(): path.read_bytes()
            for path in sorted(folder.rglob("*")) if path.is_file()}


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def stop_gnupg():
    """Stop the agent that signing starts, and remove its sockets' folder."""
    run("gpgconf", "--kill", "all", check=False)
    run("gpgconf", "--remove-socketdir", check=False)


def make_key(name, passphrase):
    """A new key of the owner's kind, a primary key that only certifies
    and a signing subkey, under this passphrase: its fingerprint."""
    secret = ["--pinentry-mode", "loopback", "--passphrase", passphrase]
    run("gpg", "--batch", "--quiet", *secret, "--quick-generate-key", name, "ed25519", "cert", "never")
    listing = run("gpg", "--batch", "--with-colons", "--list-keys", name).stdout
    fingerprint = next(line.split(":")[9] for line in listing.splitlines() if line.startswith("fpr:"))
    run("gpg", "--batch", "--quiet", *secret, "--quick-add-key", fingerprint, "ed25519", "sign", "1d")
    return fingerprint


def throwaway_gnupg(test_class):
    """Give a test class, from its setUpClass, a temporary ``folder`` with
    a GnuPG home of its own, whose agent asks a stand-in pinentry for
    passphrases: it gives PASSPHRASE and notes each request in
    ``pinentry_log``. The class gets two keys: ``key``, under PASSPHRASE,
    and ``other_key``, without one, with their public keyrings ``keyring``
    and ``other_keyring``. Its cleanups stop GnuPG and remove the folder."""
    temporary = tempfile.TemporaryDirectory()
    test_class.addClassCleanup(temporary.cleanup)
    test_class.folder = folder = Path(temporary.name)
    home = folder / "gnupg"
    home.mkdir(mode=0o700)
    environment = mock.patch.dict(os.environ, {"GNUPGHOME": str(home)})
    environment.start()
    test_class.addClassCleanup(environment.stop)
    test_class.addClassCleanup(stop_gnupg)     # before the folder goes
    test_class.pinentry_log = folder / "pinentry.log"
    pinentry = folder / "pinentry"
    pinentry.write_text(PINENTRY.format(python=sys.executable, log=str(test_class.pinentry_log),
                                        passphrase=PASSPHRASE))
    pinentry.chmod(0o700)
    (home / "gpg-agent.conf").write_text(f"pinentry-program {pinentry}\n")
    test_class.key = make_key("Radiolack test repository", PASSPHRASE)
    test_class.other_key = make_key("Another test repository", "")
    run("gpgconf", "--kill", "gpg-agent")     # forget the passphrase
    test_class.keyring = folder / "keyring.gpg"
    test_class.keyring.write_bytes(run("gpg", "--batch", "--export", test_class.key, text=False).stdout)
    test_class.other_keyring = folder / "other-keyring.gpg"
    test_class.other_keyring.write_bytes(run("gpg", "--batch", "--export", test_class.other_key, text=False).stdout)


@unittest.skipUnless(all(shutil.which(tool) for tool in TOOLS), "dpkg-deb, gpg, gpgv and gpgconf")
class RepositoryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        throwaway_gnupg(cls)
        cls.first = cls.make_package("1.0.0")
        cls.second = cls.make_package("1.1.0")
        cls.second_rebuilt = cls.make_package("1.1.0", note="rebuilt")

    @classmethod
    def make_package(cls, version, name="radiolack", architecture="all", note="", fields=""):
        """A small Debian package, built with dpkg-deb."""
        tree = Path(tempfile.mkdtemp(dir=cls.folder))
        (tree / "DEBIAN").mkdir()
        (tree / "DEBIAN" / "control").write_text(
            f"Package: {name}\nVersion: {version}\nArchitecture: {architecture}\n"
            "Maintainer: Test <test@example.invalid>\n"
            "Depends: python3 (>= 3.11), gir1.2-webkit2-4.1 | gir1.2-webkit2-4.0\n"
            f"{fields}Description: Test package\n Built by the tests.\n .\n A second paragraph.\n")
        documentation = tree / "usr" / "share" / "doc" / name
        documentation.mkdir(parents=True)
        (documentation / "note").write_text(f"{version} {note}\n")
        output = Path(tempfile.mkdtemp(dir=cls.folder)) / f"{name}_{version}_{architecture}.deb"
        run("dpkg-deb", "--root-owner-group", "--build", str(tree), str(output))
        return output

    def setUp(self):
        self.repository = Path(tempfile.mkdtemp(dir=self.folder)) / "repository"

    def update(self, *packages, key=None, date=DATE):
        return apt_repository.update(self.repository, key or self.key, list(packages), date)

    def apt(self, keyring=None, source=None):
        """An environment that runs apt-get and apt-cache as the current
        user, in a folder of their own, with one source: by default, the
        repository, with this keyring."""
        root = Path(tempfile.mkdtemp(dir=self.folder))
        for folder in ("etc/apt/sources.list.d", "etc/apt/apt.conf.d", "etc/apt/preferences.d",
                       "etc/apt/trusted.gpg.d", "var/lib/apt/lists/partial", "var/cache/apt/archives/partial",
                       "var/lib/dpkg", "var/log/apt"):
            (root / folder).mkdir(parents=True)
        (root / "var/lib/dpkg/status").touch()
        (root / "etc/apt/sources.list.d/radiolack.sources").write_text(source or (
            f"Types: deb\nURIs: file:{self.repository}/\nSuites: stable\nComponents: main\n"
            f"Signed-By: {keyring or self.keyring}\n"))
        (root / "apt.conf").write_text(
            f'Dir "{root}/";\nDir::State::status "{root}/var/lib/dpkg/status";\n'
            'Dir::Bin::Methods "/usr/lib/apt/methods/";\nDebug::NoLocking "true";\n'
            # Run as root, apt would fetch as the _apt user, who cannot read the temporary folder.
            f'APT::Sandbox::User "{pwd.getpwuid(os.getuid()).pw_name}";\n')
        return {**os.environ, "APT_CONFIG": str(root / "apt.conf"), "LC_ALL": "C"}

    def assertRefused(self, error, *packages, key=None):
        """The update fails with this error and leaves the repository as it was."""
        before = files(self.repository) if self.repository.exists() else {}
        with self.assertRaises(error) as caught:
            self.update(*packages, key=key)
        self.assertEqual(files(self.repository) if self.repository.exists() else {}, before)
        return str(caught.exception)

    def test_lays_out_and_signs_the_repository(self):
        packages, added = self.update(self.first)
        name = f"{POOL}/radiolack_1.0.0_all.deb"
        self.assertEqual(set(packages), {name})
        self.assertEqual(added, {name})
        content = files(self.repository)
        self.assertEqual(content[name], self.first.read_bytes())
        index = content[f"{INDEXES}/Packages"]
        self.assertEqual(gzip.decompress(content[f"{INDEXES}/Packages.gz"]), index)
        for data in (index, content[f"{INDEXES}/Packages.gz"]):
            self.assertEqual(content[f"{INDEXES}/by-hash/SHA256/{sha256(data)}"], data)
        entry = apt_repository.control_fields(index.decode())
        self.assertEqual((entry["package"], entry["version"], entry["filename"]), ("radiolack", "1.0.0", name))
        self.assertEqual((int(entry["size"]), entry["sha256"]), (len(content[name]), sha256(content[name])))
        self.assertIn("A second paragraph.", entry["description"])
        release = content["dists/stable/Release"]
        fields = apt_repository.control_fields(release.decode())
        self.assertEqual(fields["date"], "Tue, 10 Nov 2026 12:00:00 +0000")
        self.assertEqual((fields["suite"], fields["codename"], fields["architectures"], fields["components"]),
                         ("stable", "stable", "all", "main"))
        self.assertEqual(fields["acquire-by-hash"], "yes")
        self.assertIn(f" {sha256(index)} {len(index)} main/binary-all/Packages\n", release.decode())
        keyring = self.keyring.read_bytes()
        self.assertEqual(apt_repository.verified(content["dists/stable/InRelease"], keyring), release)
        self.assertEqual(apt_repository.verified(content["dists/stable/Release.gpg"], keyring, release), release)
        for path in self.repository.rglob("*"):
            self.assertFalse(path.name.endswith(".partial"), path)
            if path.is_file():
                self.assertEqual(path.stat().st_mode & 0o777, 0o644, path)

    def test_asks_for_the_passphrase_as_gnupg_does(self):
        run("gpgconf", "--kill", "gpg-agent")
        asked = self.pinentry_log.read_text().count("asked") if self.pinentry_log.exists() else 0
        self.update(self.first)
        self.assertGreater(self.pinentry_log.read_text().count("asked"), asked)

    def test_adds_packages_and_keeps_the_old_indexes(self):
        self.update(self.first)
        old_index = (self.repository / INDEXES / "Packages").read_bytes()
        packages, added = self.update(self.second, self.first, date=DATE + timedelta(days=1))
        self.assertEqual(set(packages), {f"{POOL}/radiolack_1.0.0_all.deb", f"{POOL}/radiolack_1.1.0_all.deb"})
        self.assertEqual(added, {f"{POOL}/radiolack_1.1.0_all.deb"})
        index = (self.repository / INDEXES / "Packages").read_text()
        self.assertEqual([line for line in index.splitlines() if line.startswith("Version:")],
                         ["Version: 1.0.0", "Version: 1.1.0"])
        self.assertEqual((self.repository / INDEXES / "by-hash" / "SHA256" / sha256(old_index)).read_bytes(),
                         old_index)

    def test_signs_again_without_new_packages(self):
        self.update(self.first)
        index = (self.repository / INDEXES / "Packages").read_bytes()
        packages, added = self.update(date=DATE + timedelta(days=400))
        self.assertEqual((set(packages), added), ({f"{POOL}/radiolack_1.0.0_all.deb"}, set()))
        self.assertEqual((self.repository / INDEXES / "Packages").read_bytes(), index)
        self.assertIn("Date: Wed, 15 Dec 2027", (self.repository / "dists/stable/InRelease").read_text())

    def test_a_published_version_never_changes(self):
        self.assertIn("a published version never changes",
                      self.assertRefused(ValueError, self.second, self.second_rebuilt))
        self.update(self.second)
        self.assertIn("a published version never changes", self.assertRefused(ValueError, self.second_rebuilt))

    def test_refuses_packages_it_does_not_publish(self):
        other = self.make_package("1.0.0", name="radiolack-extra")
        binary = self.make_package("1.0.0", architecture="amd64")
        indexed = self.make_package("1.0.0", fields="SHA256: 0000\n")
        text = self.folder / "notes.deb"
        text.write_text("not a package\n")
        for package, message in ((other, "not radiolack"), (binary, "packages only"),
                                 (indexed, "only the index may give"), (text, "not a Debian package")):
            with self.subTest(package=package.name):
                self.assertIn(message, self.assertRefused(ValueError, package))
        self.assertRefused(OSError, self.folder / "missing.deb")
        self.assertIn("no package yet", self.assertRefused(ValueError))

    def test_refuses_a_pool_that_differs_from_the_signed_index(self):
        self.update(self.first, self.second)
        pool = self.repository / POOL
        changes = {
            "is not the package that the signed index lists":
                lambda: shutil.copyfile(self.second_rebuilt, pool / "radiolack_1.1.0_all.deb"),
            "is missing from the pool": lambda: (pool / "radiolack_1.0.0_all.deb").unlink(),
            "does not belong in the pool": lambda: (pool / "notes.txt").write_text("notes\n"),
            "is in no signed index":
                lambda: shutil.copyfile(self.make_package("1.2.0"), pool / "radiolack_1.2.0_all.deb"),
        }
        for message, change in changes.items():
            with self.subTest(message=message):
                before = files(self.repository)
                change()
                self.assertIn(message, self.assertRefused(ValueError))
                shutil.rmtree(pool)
                for name, data in before.items():
                    (self.repository / name).parent.mkdir(parents=True, exist_ok=True)
                    (self.repository / name).write_bytes(data)
        self.update()     # the repository as it was signs again

    def test_finishes_an_update_that_stopped_before_signing(self):
        self.update(self.first)
        shutil.copyfile(self.second, self.repository / POOL / "radiolack_1.1.0_all.deb")
        self.assertRefused(ValueError)
        packages, added = self.update(self.second)
        self.assertEqual(len(packages), 2)
        self.assertEqual(added, set())

    def test_refuses_a_repository_that_another_key_signed(self):
        self.update(self.first, key=self.other_key)
        self.assertIn("signature does not check out", self.assertRefused(ValueError, self.second))
        release = self.repository / "dists/stable/InRelease"
        release.write_text(release.read_text().replace("Suite: stable", "Suite: other", 1))
        self.assertIn("signature does not check out", self.assertRefused(ValueError, key=self.other_key))

    def test_refuses_an_index_that_differs_from_its_release_file(self):
        self.update(self.first)
        indexes = self.repository / INDEXES
        index = indexes / "by-hash" / "SHA256" / sha256((indexes / "Packages").read_bytes())
        index.write_text(index.read_text().replace("Version: 1.0.0", "Version: 9.0.0"))
        self.assertIn("is not the index that", self.assertRefused(ValueError, self.second))

    def test_refuses_symbolic_links(self):
        elsewhere = Path(tempfile.mkdtemp(dir=self.folder))
        self.repository.mkdir()
        (self.repository / "dists").symlink_to(elsewhere)
        self.assertIn("symbolic link", self.assertRefused(ValueError, self.first))
        (self.repository / "dists").unlink()
        (self.repository / "pool").mkdir()
        (self.repository / "pool" / "main").symlink_to(elsewhere)
        self.assertIn("symbolic link", self.assertRefused(ValueError, self.first))
        self.assertEqual(list(elsewhere.iterdir()), [])

    def test_signs_only_with_a_key_named_by_its_fingerprint(self):
        listing = run("gpg", "--batch", "--with-colons", "--list-keys", self.key).stdout
        long_id = next(line.split(":")[4] for line in listing.splitlines() if line.startswith("pub:"))
        for key in (long_id, "Radiolack test repository", self.key + "!"):
            with self.subTest(key=key):
                self.assertIn("whole fingerprint", self.assertRefused(ValueError, self.first, key=key))
        self.assertIn("GnuPG has no key", self.assertRefused(RuntimeError, self.first, key="0" * 40))

    def test_the_pool_leaves_out_the_epoch(self):
        package = apt_repository.Package("1:2.0.0~dev0", "", b"", "")
        self.assertEqual(package.pool_path, f"{POOL}/radiolack_2.0.0~dev0_all.deb")

    def test_reads_control_paragraphs(self):
        fields = apt_repository.control_fields("Package: radiolack\nDescription: Short\n Long\n .\n More\n")
        self.assertEqual(fields, {"package": "radiolack", "description": "Short\n Long\n .\n More"})
        for paragraph in ("Package: radiolack\n\nPackage: other\n", "Package: a\npackage: b\n", " Orphan\n",
                          "No colon\n"):
            with self.subTest(paragraph=paragraph), self.assertRaises(ValueError):
                apt_repository.control_fields(paragraph)

    def test_the_command_adds_packages_and_signs(self):
        command = [sys.executable, str(ROOT / "packaging" / "apt_repository.py"), "--key", self.key,
                   str(self.repository), str(self.first)]
        result = run(*command)
        self.assertEqual(result.stdout.splitlines(), [
            f"added radiolack 1.0.0: {POOL}/radiolack_1.0.0_all.deb",
            f"signed {self.repository}/dists/stable/InRelease with {self.key}",
        ])
        result = run(*command[:3], "0" * 40, *command[4:], check=False)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stderr, f"apt_repository: GnuPG has no key {'0' * 40}\n")

    @unittest.skipUnless(APT, "apt-get, as on Debian and Ubuntu")
    def test_apt_reads_the_repository_and_its_packages(self):
        self.update(self.first)
        self.update(self.second)
        environment = self.apt()
        run("apt-get", "update", env=environment)
        policy = run("apt-cache", "policy", "radiolack", env=environment).stdout
        self.assertIn("Candidate: 1.1.0", policy)
        self.assertIn("1.0.0 500", policy)
        download = Path(tempfile.mkdtemp(dir=self.folder))
        run("apt-get", "download", "radiolack", env=environment, cwd=download)
        self.assertEqual((download / "radiolack_1.1.0_all.deb").read_bytes(), self.second.read_bytes())

    @unittest.skipUnless(APT, "apt-get, as on Debian and Ubuntu")
    def test_apt_refuses_what_the_key_did_not_sign(self):
        self.update(self.first)
        indexes = self.repository / INDEXES
        text = (indexes / "Packages").read_bytes()
        changed = text.replace(b"Version: 1.0.0", b"Version: 9.0.0")
        release = self.repository / "dists/stable/InRelease"
        package = self.repository / POOL / "radiolack_1.0.0_all.deb"

        def change_the_index():
            for name, data in (("Packages", changed), ("Packages.gz", gzip.compress(changed))):
                original = (indexes / name).read_bytes()
                (indexes / name).write_bytes(data)
                (indexes / "by-hash" / "SHA256" / sha256(original)).write_bytes(data)

        cases = {
            "index": change_the_index,
            "release file": lambda: release.write_text(release.read_text().replace("Label: Radiolack", "Label: Other")),
            "key": lambda: None,
        }
        original = files(self.repository)
        for case, change in cases.items():
            with self.subTest(case=case):
                change()
                environment = self.apt(self.other_keyring if case == "key" else None)
                result = run("apt-get", "update", env=environment, check=False)
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertNotIn("Candidate", run("apt-cache", "policy", "radiolack", env=environment).stdout)
                for name, data in original.items():
                    (self.repository / name).write_bytes(data)
        environment = self.apt()
        run("apt-get", "update", env=environment)
        package.write_bytes(package.read_bytes() + b"\0")
        download = Path(tempfile.mkdtemp(dir=self.folder))
        result = run("apt-get", "download", "radiolack", env=environment, cwd=download, check=False)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse((download / "radiolack_1.0.0_all.deb").exists())

    @unittest.skipUnless(APT, "apt-get, as on Debian and Ubuntu")
    def test_apt_reads_the_source_that_the_package_adds(self):
        source = (ROOT / "packaging" / "radiolack.sources").read_text(encoding="utf-8")
        fields = dict(line.split(": ", 1) for line in source.splitlines() if line and not line.startswith("#"))
        result = run("apt-get", "update", "--print-uris", env=self.apt(source=source))
        self.assertIn(f"'{fields['URIs']}dists/stable/InRelease' ", result.stdout)
        # Pointed at this repository and its key, apt fetches the repository and checks it.
        self.update(self.first)
        local = source.replace(f"URIs: {fields['URIs']}\n", f"URIs: file:{self.repository}/\n").replace(
            f"Signed-By: {fields['Signed-By']}\n", f"Signed-By: {self.keyring}\n")
        environment = self.apt(source=local)
        run("apt-get", "update", env=environment)
        self.assertIn("Candidate: 1.0.0", run("apt-cache", "policy", "radiolack", env=environment).stdout)
        # Turned off, the source gives apt nothing to fetch.
        environment = self.apt(source=local.replace("\nEnabled: yes\n", "\nEnabled: no\n"))
        self.assertEqual(run("apt-get", "update", "--print-uris", env=environment).stdout, "")


if __name__ == "__main__":
    unittest.main()
