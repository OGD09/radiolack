"""Tests for the Debian package (packaging/build_deb.py): its version, its
control fields, its files and their dates, built into a temporary folder,
twice, to compare. The launcher it installs runs from the extracted package,
without a window, through packaging/smoke_test.py. Building needs dpkg-deb,
as on Debian and Ubuntu."""

import contextlib
import email.utils
import gzip
import importlib.util
import io
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import radiolack
from radiolack import cli

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("build_deb", ROOT / "packaging" / "build_deb.py")
build_deb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build_deb)


class VersionTest(unittest.TestCase):
    def test_a_development_version_comes_before_its_release(self):
        self.assertEqual(build_deb.debian_version("1.2.0"), "1.2.0")
        self.assertEqual(build_deb.debian_version("1.2.0.dev0"), "1.2.0~dev0")
        self.assertEqual(build_deb.debian_version("1.2.0.dev0", "git20261110084512.938a320"),
                         "1.2.0~dev0+git20261110084512.938a320")
        self.assertEqual(build_deb.debian_version("1.2.0", "git20261110084512.938a320"), "1.2.0")

    def test_refuses_other_versions(self):
        for version in ("1.2.0rc1", "1.2.0.post1", "v1.2", ""):
            with self.subTest(version=version), self.assertRaises(ValueError):
                build_deb.debian_version(version)

    def test_reads_the_package_version(self):
        self.assertEqual(build_deb.python_version(), radiolack.__version__)


class DateTest(unittest.TestCase):
    def test_source_date_epoch_dates_the_package(self):
        self.assertEqual(build_deb.source_date_epoch({"SOURCE_DATE_EPOCH": " 1700000000 "}), 1700000000)

    def test_refuses_other_dates(self):
        for value in ("-1", "1.5", "now", "1e9", str(8**11)):
            with self.subTest(value=value), self.assertRaises(ValueError):
                build_deb.source_date_epoch({"SOURCE_DATE_EPOCH": value})

    def test_otherwise_the_last_commit_dates_the_package(self):
        commit = build_deb.last_commit()
        if commit is None:
            self.skipTest("not in a git checkout")
        self.assertEqual(build_deb.source_date_epoch({"SOURCE_DATE_EPOCH": ""}), commit[0])

    def test_outside_a_git_checkout_the_package_is_dated_now(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertIsNone(build_deb.last_commit(Path(folder)))
            before = int(time.time())
            epoch = build_deb.source_date_epoch({}, Path(folder))
            self.assertTrue(before <= epoch <= time.time())


class DocumentationTest(unittest.TestCase):
    def test_compresses_as_gzip_does_for_debian(self):
        for text in ("", "Radiolack 1.2.0, ±12.5 kHz\n" * 50):
            with self.subTest(length=len(text)):
                data = build_deb._compress(text)
                self.assertEqual(gzip.decompress(data).decode("utf-8"), text)
                self.assertEqual(data[:4], b"\x1f\x8b\x08\x00")    # gzip, deflate, no name
                self.assertEqual(data[4:8], bytes(4))                 # no date
                self.assertEqual(data[8:10], b"\x02\x03")            # best compression, Unix
                self.assertEqual(build_deb._compress(text), data)

    def test_the_changelog_names_the_version_the_maintainer_and_the_date(self):
        entry = build_deb.changelog("1.2.0", " A Name <a@example.org> ", 1700000000)
        lines = entry.splitlines()
        self.assertEqual(lines[:4], ["radiolack (1.2.0) unstable; urgency=medium", "", "  * Radiolack 1.2.0.", ""])
        signature, date = lines[4].split("  ")
        self.assertEqual(signature, " -- A Name <a@example.org>")
        self.assertEqual(email.utils.parsedate_to_datetime(date).timestamp(), 1700000000)
        self.assertEqual((len(lines), entry[-1]), (5, "\n"))
        with self.assertRaises(ValueError):
            build_deb.changelog("1.2.0", "a@example.org", 1700000000)

    @unittest.skipUnless(shutil.which("dpkg-parsechangelog"), "dpkg-parsechangelog (dpkg-dev)")
    def test_dpkg_reads_the_changelog(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "changelog"
            path.write_text(build_deb.changelog("1.2.0~dev0+git20231114221320.938a320", "A Name <a@example.org>",
                                                1700000000), encoding="utf-8")
            result = subprocess.run(["dpkg-parsechangelog", "-l", str(path)], capture_output=True, text=True,
                                    timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        fields = dict(line.split(": ", 1) for line in result.stdout.splitlines() if ": " in line)
        self.assertEqual(fields["Version"], "1.2.0~dev0+git20231114221320.938a320")
        self.assertEqual(fields["Maintainer"], "A Name <a@example.org>")
        self.assertEqual(fields["Timestamp"], "1700000000")

    def test_the_manual_page_documents_every_option_and_no_other(self):
        page = build_deb.manual_page("1.2.0", 1700000000)
        self.assertIn('\n.TH RADIOLACK 1 "2023-11-14" "Radiolack 1.2.0" "User Commands"\n', page)
        self.assertNotIn("@", page)
        self.assertTrue(page.isascii())
        options = {option for action in cli.build_parser()._actions for option in action.option_strings}
        self.assertTrue({"--help", "--version", "--ui", "--port", "--rtl-sdr", "--rtl-tcp"} <= options)
        documented = {name.replace("\\-", "-") for name in re.findall(r"\\fB((?:\\-)+[a-z](?:[a-z]|\\-)*)\\fR", page)}
        self.assertEqual(documented, options)


class MaintainerTest(unittest.TestCase):
    def test_the_maintainer_comes_from_both_variables(self):
        self.assertEqual(build_deb.default_maintainer({"DEBFULLNAME": "A Name", "DEBEMAIL": "a@example.org"}),
                         "A Name <a@example.org>")
        self.assertIsNone(build_deb.default_maintainer({"DEBEMAIL": "a@example.org"}))

    def test_the_maintainer_is_a_name_and_an_address_on_one_line(self):
        self.assertEqual(build_deb.maintainer_field(" A Name <a@example.org> "), "A Name <a@example.org>")
        for maintainer in ("", "A Name", "a@example.org", "<a@example.org>", "A Name <a@example.org",
                           "A Name <a@example.org>\nEssential: yes"):
            with self.subTest(maintainer=maintainer), self.assertRaises(ValueError):
                build_deb.maintainer_field(maintainer)

    def test_builds_nothing_without_a_valid_maintainer(self):
        # dpkg warns about an installed package without a maintainer each time it reads its database.
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "dist"
            with mock.patch.dict(os.environ, {"DEBFULLNAME": "", "DEBEMAIL": ""}), \
                    contextlib.redirect_stderr(io.StringIO()) as errors, self.assertRaises(SystemExit) as stop:
                build_deb.main(["--output", str(output)])
            self.assertEqual(stop.exception.code, 2)
            self.assertIn("a maintainer is needed", errors.getvalue())
            with contextlib.redirect_stderr(io.StringIO()) as errors:
                status = build_deb.main(["--output", str(output), "--maintainer", "a@example.org"])
            self.assertEqual(status, 1)
            self.assertIn('the maintainer must read "Name <address>"', errors.getvalue())
            self.assertFalse(output.exists())


@unittest.skipUnless(shutil.which("dpkg-deb"), "dpkg-deb, as on Debian and Ubuntu")
class PackageTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        temporary = tempfile.TemporaryDirectory()
        cls.addClassCleanup(temporary.cleanup)
        cls.folder = Path(temporary.name)
        cls.package = build_deb.build(cls.folder / "dist", maintainer="A Name <a@example.org>")
        cls.epoch = build_deb.source_date_epoch()
        cls.tree = cls.folder / "tree"
        subprocess.run(["dpkg-deb", "-x", str(cls.package), str(cls.tree)], check=True, timeout=60)

    def field(self, name: str) -> str:
        return subprocess.run(["dpkg-deb", "--field", str(self.package), name], capture_output=True, text=True,
                              check=True, timeout=60).stdout.strip()

    def control(self, name: str) -> str:
        """One of the package's control files."""
        return subprocess.run(["dpkg-deb", "--info", str(self.package), name], capture_output=True, text=True,
                               check=True, timeout=60).stdout

    def test_describes_an_architecture_independent_package(self):
        self.assertEqual((self.field("Package"), self.field("Architecture")), ("radiolack", "all"))
        self.assertTrue(self.field("Version").startswith(build_deb.debian_version(radiolack.__version__)))
        self.assertEqual(self.package.name, f"radiolack_{self.field('Version')}_all.deb")
        self.assertEqual(self.field("Maintainer"), "A Name <a@example.org>")
        depends = self.field("Depends")
        for needed in ("python3 (>= 3.11)", "python3-aiohttp", "python3-cryptography", "python3-numpy", "python3-gi",
                       "gir1.2-gtk-3.0", "gir1.2-webkit2-4.1", "ca-certificates"):
            self.assertIn(needed, depends)
        self.assertEqual(self.field("Recommends"), "rtl-sdr")

    def test_installs_the_command_the_menu_entry_and_the_icon(self):
        listing = subprocess.run(["dpkg-deb", "--contents", str(self.package)], capture_output=True, text=True,
                                 check=True, timeout=60).stdout.splitlines()
        modes = {line.split()[-1].removeprefix("."): (line.split()[0], line.split()[1]) for line in listing}
        self.assertEqual(modes["/usr/bin/radiolack"], ("-rwxr-xr-x", "root/root"))
        for path in ("/usr/share/applications/radiolack.desktop",
                     "/usr/share/icons/hicolor/scalable/apps/radiolack.svg",
                     "/usr/share/doc/radiolack/copyright",
                     "/usr/lib/python3/dist-packages/radiolack/cli.py",
                     "/usr/lib/python3/dist-packages/radiolack/web/index.html"):
            self.assertEqual(modes[path], ("-rw-r--r--", "root/root"), path)
        self.assertFalse([path for path in modes if "__pycache__" in path or path.endswith(".pyc")])
        # The packaged command, not a radiolack found earlier on the PATH, such as in ~/.local/bin.
        entry = (self.tree / "usr/share/applications/radiolack.desktop").read_text(encoding="utf-8")
        self.assertIn("\nExec=/usr/bin/radiolack\n", entry)

    def test_adds_the_apt_repository_as_configuration_files(self):
        source, keyring = "etc/apt/sources.list.d/radiolack.sources", "etc/apt/keyrings/radiolack-archive-keyring.gpg"
        self.assertEqual(self.control("conffiles").splitlines(), [f"/{keyring}", f"/{source}"])
        for path, name in ((source, "radiolack.sources"), (keyring, "radiolack-archive-keyring.gpg")):
            self.assertEqual((self.tree / path).read_bytes(), (ROOT / "packaging" / name).read_bytes(), path)
            self.assertEqual((self.tree / path).stat().st_mode & 0o777, 0o644, path)
        self.assertIn(f"\nSigned-By: /{keyring}\n", (self.tree / source).read_text(encoding="utf-8"))
        # As Debian's tools write md5sums: every file but the conffiles, whose checksums dpkg keeps apart.
        listed = {line.split("  ", 1)[1] for line in self.control("md5sums").splitlines()}
        installed = {path.relative_to(self.tree).as_posix() for path in self.tree.rglob("*") if path.is_file()}
        self.assertEqual(listed, installed - {source, keyring})
        # Lintian flags every package that adds an APT source; this one does on purpose.
        overrides = (self.tree / "usr/share/lintian/overrides/radiolack").read_text(encoding="utf-8")
        self.assertIn(f"\nradiolack: package-installs-apt-sources [{source}]\n", overrides)

    def test_states_the_licence(self):
        # Debian keeps a package's licence notice in /usr/share/doc/PACKAGE/copyright, and the text of the
        # GPL, which the repository's LICENSE holds, in /usr/share/common-licenses.
        notice = (self.tree / "usr/share/doc/radiolack/copyright").read_text(encoding="utf-8")
        self.assertTrue(notice.startswith("Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/\n"))
        self.assertIn("\nFiles: *\n", notice)
        self.assertIn("\nLicense: GPL-3+\n", notice)
        self.assertIn('"/usr/share/common-licenses/GPL-3"', notice)
        licence = (ROOT / "LICENSE").read_text(encoding="utf-8")
        self.assertTrue(licence.lstrip().startswith("GNU GENERAL PUBLIC LICENSE"))
        self.assertIn("Version 3, 29 June 2007", licence)

    def test_carries_every_file_of_the_python_package(self):
        packaged = self.tree / "usr/lib/python3/dist-packages/radiolack"
        source = ROOT / "src/radiolack"
        expected = {path.relative_to(source) for path in source.rglob("*")
                    if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"}
        self.assertEqual({path.relative_to(packaged) for path in packaged.rglob("*") if path.is_file()}, expected)

    def test_the_command_runs_the_packaged_radiolack(self):
        packaged = self.tree / "usr/lib/python3/dist-packages"
        environment = {**os.environ, "PYTHONPATH": str(packaged)}
        result = subprocess.run([sys.executable, str(self.tree / "usr/bin/radiolack"), "--version"],
                                capture_output=True, text=True, env=environment, timeout=60)
        self.assertEqual((result.returncode, result.stdout.strip()), (0, f"radiolack {radiolack.__version__}"))
        where = subprocess.run([sys.executable, "-c", "import radiolack; print(radiolack.__file__)"],
                               capture_output=True, text=True, env=environment, check=True, timeout=60).stdout
        self.assertTrue(Path(where.strip()).is_relative_to(packaged))

    def test_documents_the_command_and_its_versions(self):
        for path in ("usr/share/man/man1/radiolack.1.gz", "usr/share/doc/radiolack/changelog.gz"):
            self.assertEqual((self.tree / path).stat().st_mode & 0o777, 0o644, path)
        version = self.field("Version")
        page = gzip.decompress((self.tree / "usr/share/man/man1/radiolack.1.gz").read_bytes()).decode("utf-8")
        self.assertIn(f'"Radiolack {version}"', page)
        entry = gzip.decompress((self.tree / "usr/share/doc/radiolack/changelog.gz").read_bytes()).decode("utf-8")
        self.assertTrue(entry.startswith(f"radiolack ({version}) unstable; urgency=medium\n"))
        self.assertIn(" -- A Name <a@example.org>  ", entry)

    def test_every_file_carries_the_date_of_the_source(self):
        members = ar_members(self.package)
        self.assertEqual(list(members), ["debian-binary", "control.tar.xz", "data.tar.xz"])
        self.assertEqual({date for date, _ in members.values()}, {self.epoch})
        for name in ("control.tar.xz", "data.tar.xz"):
            with tarfile.open(fileobj=io.BytesIO(members[name][1])) as archive:
                entries = archive.getmembers()
            self.assertTrue(entries)
            for entry in entries:
                with self.subTest(archive=name, entry=entry.name):
                    self.assertEqual(entry.mtime, self.epoch)
                    self.assertEqual((entry.uid, entry.gid, entry.uname, entry.gname), (0, 0, "root", "root"))

    def test_a_second_build_is_identical(self):
        with tempfile.TemporaryDirectory() as folder:
            again = build_deb.build(Path(folder), maintainer="A Name <a@example.org>")
            self.assertEqual(again.name, self.package.name)
            self.assertEqual(again.read_bytes(), self.package.read_bytes())

    def test_the_packaged_radiolack_passes_the_smoke_test(self):
        # Radiolack from the package's files, on an ephemeral port, in a settings folder of the smoke test's own.
        environment = {**os.environ, "PYTHONPATH": str(self.tree / "usr/lib/python3/dist-packages")}
        result = subprocess.run([sys.executable, str(ROOT / "packaging/smoke_test.py"), "--",
                                 sys.executable, str(self.tree / "usr/bin/radiolack")],
                                capture_output=True, text=True, env=environment, timeout=300)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"ok: prints its version: radiolack {radiolack.__version__}\n", result.stdout)
        # Only the installed package's Radiolack checks for updates.
        self.assertIn("ok: checks for updates at start: unavailable (Radiolack checks for updates when it runs "
                      "from its Debian package.)\n", result.stdout)
        self.assertIn("smoke_test: every check passed\n", result.stdout)

    @unittest.skipUnless(shutil.which("desktop-file-validate"), "desktop-file-validate (desktop-file-utils)")
    def test_the_menu_entry_is_valid(self):
        entry = self.tree / "usr/share/applications/radiolack.desktop"
        result = subprocess.run(["desktop-file-validate", str(entry)], capture_output=True, text=True, timeout=60)
        self.assertEqual((result.returncode, result.stdout + result.stderr), (0, ""))


def ar_members(path: Path) -> dict[str, tuple[int, bytes]]:
    """The members of a Debian package, an ar archive: name -> (date, content)."""
    data = path.read_bytes()
    if not data.startswith(b"!<arch>\n"):
        raise ValueError(f"{path} is not an ar archive")
    members, offset = {}, 8
    while offset < len(data):
        header = data[offset:offset + 60]
        name = header[:16].decode("ascii").rstrip().removesuffix("/")
        date, size = int(header[16:28]), int(header[48:58])
        members[name] = (date, data[offset + 60:offset + 60 + size])
        offset += 60 + size + size % 2
    return members


if __name__ == "__main__":
    unittest.main()
