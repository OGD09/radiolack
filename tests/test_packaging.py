"""Tests for the Debian package (packaging/build_deb.py): its version, its
control fields and its files, built into a temporary folder. The launcher it
installs runs from the extracted package, without a window. Building needs
dpkg-deb, as on Debian and Ubuntu."""

import contextlib
import importlib.util
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import radiolack

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
        cls.tree = cls.folder / "tree"
        subprocess.run(["dpkg-deb", "-x", str(cls.package), str(cls.tree)], check=True, timeout=60)

    def field(self, name: str) -> str:
        return subprocess.run(["dpkg-deb", "--field", str(self.package), name], capture_output=True, text=True,
                              check=True, timeout=60).stdout.strip()

    def test_describes_an_architecture_independent_package(self):
        self.assertEqual((self.field("Package"), self.field("Architecture")), ("radiolack", "all"))
        self.assertTrue(self.field("Version").startswith(build_deb.debian_version(radiolack.__version__)))
        self.assertEqual(self.package.name, f"radiolack_{self.field('Version')}_all.deb")
        self.assertEqual(self.field("Maintainer"), "A Name <a@example.org>")
        depends = self.field("Depends")
        for needed in ("python3 (>= 3.11)", "python3-aiohttp", "python3-numpy", "python3-gi", "gir1.2-gtk-3.0",
                       "gir1.2-webkit2-4.1"):
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

    @unittest.skipUnless(shutil.which("desktop-file-validate"), "desktop-file-validate (desktop-file-utils)")
    def test_the_menu_entry_is_valid(self):
        entry = self.tree / "usr/share/applications/radiolack.desktop"
        result = subprocess.run(["desktop-file-validate", str(entry)], capture_output=True, text=True, timeout=60)
        self.assertEqual((result.returncode, result.stdout + result.stderr), (0, ""))


if __name__ == "__main__":
    unittest.main()
