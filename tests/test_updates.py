"""Tests for the update check (updates.py): when it is unavailable, what it
makes of apt's answers, and how it stops apt when apt takes too long or the
check is cancelled, with stand-ins for apt-get and apt-cache; then with apt
itself, run as the current user against repositories that throwaway keys
signed (tests/test_apt_repository.py). Neither touches the system's apt
or its package lists, nor the network."""

import asyncio
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from radiolack.updates import UpdateCheck, UpdateResult
from tests.test_apt_repository import APT, TOOLS, apt_repository, run, throwaway_gnupg

DPKG = os.access("/usr/bin/dpkg", os.X_OK)
DATE = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)
SOURCE = "Types: deb\nURIs: file:{repository}/\nSuites: stable\nComponents: main\nSigned-By: {keyring}\n"
DPKG_STATUS = ("Package: radiolack\nStatus: install ok installed\nPriority: optional\nSection: hamradio\n"
               "Maintainer: Test <test@example.invalid>\nArchitecture: all\nVersion: {version}\n"
               "Description: Test package\n Installed for the tests.\n")
POLICY = "radiolack:\n  Installed: {installed}\n  Candidate: {candidate}\n  Version table:\n"
NOT_PACKAGED = "Radiolack checks for updates when it runs from its Debian package."
# Stands in for apt-get, apt-cache and dpkg: does what behaviour.json, beside
# it, says for its name, and notes how it ran in NAME.json.
FAKE_PROGRAM = """#!{python}
import json, os, subprocess, sys, time
from pathlib import Path
me = Path(sys.argv[0])
behaviour = json.loads((me.parent / "behaviour.json").read_text())[me.name]
options = dict(argument.split("=", 1) for argument in sys.argv[1:] if "=" in argument)
config = os.environ.get("APT_CONFIG")
me.with_name(me.name + ".json").write_text(json.dumps({{
    "arguments": sys.argv[1:], "options": options, "lc_all": os.environ.get("LC_ALL"),
    "apt_config": Path(config).read_text() if config else None}}))
if behaviour.get("child"):
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    (me.parent / "child.pid").write_text(str(child.pid))
if behaviour.get("index"):
    lists = Path(options["Dir::State::Lists"])
    (lists / "example.invalid_dists_stable_main_binary-all_Packages").write_text("Package: radiolack\\n")
time.sleep(behaviour.get("sleep", 0))
print(behaviour.get("output", ""), end="")
sys.exit(behaviour.get("code", 0))
"""


def ended(pid: int, timeout_s: float = 5.0) -> bool:
    """Whether a process has ended, as a zombie or gone, within the time."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            if Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] == "Z":
                return True
        except (FileNotFoundError, ProcessLookupError):
            return True
        time.sleep(0.02)
    return False


class Installation(unittest.TestCase):
    """A Radiolack installed as the Debian package installs it, in a
    temporary folder: its module (``package``), whose version is 1.0.0, and
    its APT source (``source``)."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        self.package = self.folder / "radiolack"
        self.package.mkdir()
        self.install("1.0.0")
        self.source = self.folder / "radiolack.sources"
        self.source.write_text(SOURCE.format(repository=self.folder / "repository",
                                             keyring=self.folder / "keyring.gpg"))
        # The check's temporary folders go here, to see that it removes them.
        self.temporary = self.folder / "tmp"
        self.temporary.mkdir()
        patch = mock.patch.object(tempfile, "tempdir", str(self.temporary))
        patch.start()
        self.addCleanup(patch.stop)

    def install(self, version: str) -> None:
        """The package's files hold this version: the next start runs it."""
        (self.package / "__init__.py").write_text(f'"""Radiolack."""\n\n__version__ = "{version}"\n')

    def checker(self, version: str = "1.0.0", **options) -> UpdateCheck:
        """The check of the Radiolack ``version`` that runs from the package."""
        return UpdateCheck(version=version, module_dir=self.package, package_dir=self.package,
                           source=self.source, **options)

    def check(self, checker: UpdateCheck) -> UpdateResult:
        result = asyncio.run(checker.check())
        self.assertEqual(list(self.temporary.iterdir()), [], "the check left its temporary folder")
        return result


class UnavailableTest(Installation):
    def test_only_the_debian_packages_radiolack_checks(self):
        checker = UpdateCheck(module_dir=self.folder, package_dir=self.package, source=self.source)
        self.assertEqual(checker.unavailable_reason(), NOT_PACKAGED)
        self.assertEqual(self.check(checker), UpdateResult("unavailable", detail=NOT_PACKAGED))
        # By default, the Radiolack that the tests run, from the checkout or pip, does not check.
        self.assertEqual(UpdateCheck().unavailable_reason(), NOT_PACKAGED)

    def test_needs_the_apt_source_in_place_and_turned_on(self):
        self.assertIsNone(self.checker().unavailable_reason())
        for source, turned_off in (("Types: deb\nEnabled: no\n", True), ("Types: deb\nenabled:  False \n", True),
                                   ("# Enabled: no\nTypes: deb\n", False), ("Types: deb\n Enabled: no\n", False),
                                   ("Types: deb\nEnabled: yes\n", False)):
            with self.subTest(source=source):
                self.source.write_text(source)
                reason = self.checker().unavailable_reason()
                self.assertEqual(reason, f"Radiolack's APT source is turned off: {self.source}." if turned_off else None)
        self.source.unlink()
        reason = f"Radiolack's APT source is missing: {self.source}."
        self.assertEqual(self.check(self.checker()), UpdateResult("unavailable", detail=reason))

    def test_needs_apt(self):
        checker = self.checker(apt_get=str(self.folder / "apt-get"))
        self.assertEqual(checker.unavailable_reason(), "apt-get is not installed.")


@unittest.skipUnless(DPKG, "dpkg, as on Debian and Ubuntu")
class AnswersTest(Installation):
    """apt-get and apt-cache stand-ins give the answers; dpkg compares the
    versions."""

    def setUp(self):
        super().setUp()
        self.programs = self.folder / "programs"
        self.programs.mkdir()
        for name in ("apt-get", "apt-cache"):
            program = self.programs / name
            program.write_text(FAKE_PROGRAM.format(python=sys.executable))
            program.chmod(0o755)
        self.behave(update={"index": True}, policy={"output": POLICY.format(installed="1.0.0", candidate="1.0.0")})

    def behave(self, update: dict, policy: dict) -> None:
        (self.programs / "behaviour.json").write_text(json.dumps({"apt-get": update, "apt-cache": policy}))

    def ran(self, name: str) -> dict:
        return json.loads((self.programs / f"{name}.json").read_text())

    def checker(self, version: str = "1.0.0", **options) -> UpdateCheck:
        return super().checker(version, apt_get=str(self.programs / "apt-get"),
                               apt_cache=str(self.programs / "apt-cache"), **options)

    def test_a_newer_version_in_the_repository_is_available(self):
        self.behave(update={"index": True}, policy={"output": POLICY.format(installed="1.0.0", candidate="1.1.0")})
        self.assertEqual(self.check(self.checker()), UpdateResult("available", latest="1.1.0"))

    def test_apt_reads_radiolacks_source_alone_in_a_folder_of_its_own(self):
        self.check(self.checker())
        update, policy = self.ran("apt-get"), self.ran("apt-cache")
        self.assertEqual((update["arguments"][:1], update["arguments"][-1:]), (["-q"], ["update"]))
        self.assertEqual(policy["arguments"][-2:], ["policy", "radiolack"])
        for ran in (update, policy):
            # The system's configuration stays out: apt reads this file first, then nothing else.
            self.assertEqual(ran["apt_config"], 'Dir::Etc::Main "/dev/null";\nDir::Etc::Parts "/dev/null";\n')
            self.assertEqual(ran["lc_all"], "C")
            options = ran["options"]
            self.assertEqual((options["Dir::Etc::SourceList"], options["Dir::Etc::SourceParts"]),
                             (str(self.source), "/dev/null"))
            for folder in ("Dir::State::Lists", "Dir::Cache"):
                self.assertTrue(Path(options[folder]).is_relative_to(self.temporary), options[folder])
            self.assertEqual(options["Debug::NoLocking"], "true")
            self.assertNotIn("Dir::State::status", options)     # the system's dpkg database
        self.assertEqual(policy["options"], update["options"])

    def test_the_latest_version_installed_and_running_is_current(self):
        self.install("1.1.0")
        self.behave(update={"index": True}, policy={"output": POLICY.format(installed="1.1.0", candidate="1.1.0")})
        self.assertEqual(self.check(self.checker("1.1.0")), UpdateResult("current"))

    def test_an_older_candidate_is_not_offered(self):
        self.behave(update={"index": True}, policy={"output": POLICY.format(installed="1.1.0", candidate="1.0.0")})
        self.assertEqual(self.check(self.checker()), UpdateResult("current"))

    def test_a_version_installed_since_the_start_runs_once_restarted(self):
        self.install("1.1.0")
        self.behave(update={"index": True}, policy={"output": POLICY.format(installed="1.1.0", candidate="1.1.0")})
        self.assertEqual(self.check(self.checker("1.0.0")), UpdateResult("installed", latest="1.1.0"))

    def test_without_the_installed_package_it_is_unavailable(self):
        for output in (POLICY.format(installed="(none)", candidate="1.1.0"), ""):
            with self.subTest(output=output):
                self.behave(update={"index": True}, policy={"output": output})
                self.assertEqual(self.check(self.checker()), UpdateResult(
                    "unavailable", detail="Radiolack's Debian package is not installed."))

    def test_a_failed_download_fails_with_apts_reason(self):
        output = ("Err:1 https://example.invalid stable InRelease\n  Could not resolve 'example.invalid'\n"
                  "Reading package lists...\n"
                  "W: Failed to fetch https://example.invalid/dists/stable/InRelease  Could not resolve "
                  "'example.invalid'\n"
                  "W: Some index files failed to download. They have been ignored, or old ones used instead.\n")
        # apt-get exits with 0 for an unreachable server: no index is what tells.
        self.behave(update={"output": output}, policy={"output": POLICY.format(installed="1.0.0", candidate="1.0.0")})
        self.assertEqual(self.check(self.checker()), UpdateResult("failed", detail=(
            "Failed to fetch https://example.invalid/dists/stable/InRelease Could not resolve 'example.invalid'")))
        self.behave(update={}, policy={})
        self.assertEqual(self.check(self.checker()), UpdateResult("failed", detail="The repository listed no packages."))

    def test_apt_errors_fail_the_check(self):
        self.behave(update={"index": True, "code": 100, "output": "E: The repository 'x' is not signed.\n"},
                    policy={})
        self.assertEqual(self.check(self.checker()), UpdateResult("failed", detail="The repository 'x' is not signed."))
        long = "E: " + "x" * 400 + "\n"
        self.behave(update={"index": True}, policy={"code": 100, "output": long})
        detail = self.check(self.checker()).detail
        self.assertEqual((len(detail), detail[-1]), (300, "…"))

    def test_a_check_that_takes_too_long_stops_apt_and_what_it_started(self):
        self.behave(update={"child": True, "sleep": 60}, policy={})
        start = time.monotonic()
        result = self.check(self.checker(timeout_s=1.0))
        self.assertEqual(result, UpdateResult("failed", detail="No answer within 1 s."))
        self.assertLess(time.monotonic() - start, 10)
        self.assertTrue(ended(int((self.programs / "child.pid").read_text())), "apt's child still runs")

    def test_a_cancelled_check_stops_apt_and_what_it_started(self):
        self.behave(update={"child": True, "sleep": 60}, policy={})
        child = self.programs / "child.pid"

        async def cancel() -> None:
            check = asyncio.ensure_future(self.checker().check())
            while not child.exists():
                await asyncio.sleep(0.02)
            check.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await check

        asyncio.run(asyncio.wait_for(cancel(), 10))
        self.assertTrue(ended(int(child.read_text())), "apt's child still runs")
        self.assertEqual(list(self.temporary.iterdir()), [])

    def test_apt_that_cannot_start_fails_the_check(self):
        (self.programs / "apt-get").write_text("#!/nonexistent/interpreter\n")
        self.assertEqual(self.check(self.checker()), UpdateResult(
            "failed", detail="apt could not run: No such file or directory."))


@unittest.skipUnless(APT and DPKG and all(shutil.which(tool) for tool in TOOLS),
                     "apt-get, dpkg, dpkg-deb and GnuPG, as on Debian and Ubuntu")
class AptTest(Installation):
    """apt itself reads repositories in temporary folders, signed by
    throwaway keys, with a dpkg database of the tests' own."""

    @classmethod
    def setUpClass(cls):
        throwaway_gnupg(cls)
        packages = [cls.make_package(version) for version in ("1.0.0", "1.1.0")]
        cls.signed = cls.folder / "signed"
        apt_repository.update(cls.signed, cls.key, packages, DATE)
        cls.other = cls.folder / "other"
        apt_repository.update(cls.other, cls.other_key, packages[:1], DATE)

    @classmethod
    def make_package(cls, version: str) -> Path:
        tree = Path(tempfile.mkdtemp(dir=cls.folder))
        (tree / "DEBIAN").mkdir()
        (tree / "DEBIAN" / "control").write_text(
            f"Package: radiolack\nVersion: {version}\nArchitecture: all\n"
            "Maintainer: Test <test@example.invalid>\nDescription: Test package\n Built by the tests.\n")
        output = Path(tempfile.mkdtemp(dir=cls.folder)) / f"radiolack_{version}_all.deb"
        run("dpkg-deb", "--root-owner-group", "--build", str(tree), str(output))
        return output

    def checker(self, version: str = "1.0.0", installed: str = "1.0.0", repository: Path | None = None,
                **options) -> UpdateCheck:
        self.source.write_text(SOURCE.format(repository=repository or self.signed, keyring=self.keyring))
        status = self.folder / "status"
        status.write_text(DPKG_STATUS.format(version=installed))
        return super().checker(version, status_file=status, **options)

    def test_finds_a_newer_version_in_a_signed_repository(self):
        self.assertEqual(self.check(self.checker()), UpdateResult("available", latest="1.1.0"))

    def test_the_latest_version_is_current(self):
        self.install("1.1.0")
        self.assertEqual(self.check(self.checker("1.1.0", installed="1.1.0")), UpdateResult("current"))

    def test_refuses_a_repository_that_another_key_signed(self):
        result = self.check(self.checker(repository=self.other))
        self.assertEqual(result.status, "failed")
        self.assertNotIn(result.detail, ("", "The repository listed no packages."))

    def test_a_missing_repository_fails(self):
        result = self.check(self.checker(repository=self.folder / "missing"))
        self.assertEqual(result.status, "failed")
        self.assertNotEqual(result.detail, "")


if __name__ == "__main__":
    unittest.main()
