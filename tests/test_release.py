"""Tests for the release script (packaging/release.py): its version rules,
and releases made in a temporary repository whose origin is a local bare
repository. Git runs there without the user's configuration, and the
repository's own checkout is never touched."""

import contextlib
import importlib.util
import io
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import radiolack

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("release", ROOT / "packaging" / "release.py")
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)

# A fixed identity, and none of the user's settings (signing, hooks, default branch).
GIT_ENVIRONMENT = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.org",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.org",
}


class VersionTest(unittest.TestCase):
    def test_a_release_has_three_numbers(self):
        self.assertEqual(release.parse_release("0.2.1"), (0, 2, 1))
        self.assertEqual(release.parse_release("1.10.0"), (1, 10, 0))
        for text in ("0.2", "v0.2.1", "0.2.1.dev0", "0.02.1", "0.2.1 ", ""):
            with self.subTest(text=text), self.assertRaises(ValueError):
                release.parse_release(text)

    def test_the_package_version_leads_to_a_release(self):
        self.assertEqual(release.parse_package_version("0.2.1.dev0"), (0, 2, 1))
        self.assertEqual(release.parse_package_version("0.2.1"), (0, 2, 1))
        with self.assertRaises(ValueError):
            release.parse_package_version("0.2.1rc1")

    def test_the_next_development_version_follows_the_release(self):
        self.assertEqual(release.next_development((0, 2, 0)), "0.2.1.dev0")
        self.assertEqual(release.next_development((1, 9, 9)), "1.9.10.dev0")

    def test_reads_the_package_version(self):
        self.assertEqual(release.read_version(ROOT / release.INIT), radiolack.__version__)
        release.parse_package_version(radiolack.__version__)

    def test_rewrites_the_version_line_only(self):
        with tempfile.TemporaryDirectory() as folder:
            init = Path(folder) / "__init__.py"
            init.write_text('"""Doc."""\n\n__version__ = "0.2.0.dev0"\n', encoding="utf-8")
            release.write_version(init, "0.2.0")
            self.assertEqual(init.read_text(encoding="utf-8"), '"""Doc."""\n\n__version__ = "0.2.0"\n')
            init.write_text("VERSION = 1\n", encoding="utf-8")
            with self.assertRaises(release.ReleaseError):
                release.write_version(init, "0.2.0")


@unittest.skipUnless(shutil.which("git"), "git")
class ReleaseTest(unittest.TestCase):
    def setUp(self):
        environment = mock.patch.dict(os.environ, GIT_ENVIRONMENT)
        environment.start()
        self.addCleanup(environment.stop)
        # Variables such as GIT_DIR, set when git runs these tests, would point git elsewhere.
        for name in [name for name in os.environ if name.startswith("GIT_") and name not in GIT_ENVIRONMENT]:
            del os.environ[name]
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        base = Path(folder.name)
        # A release made without root= would fail here instead of using the repository.
        root = mock.patch.object(release, "ROOT", base / "missing")
        root.start()
        self.addCleanup(root.stop)
        self.origin, self.clone = base / "origin.git", base / "clone"
        self.git(base, "init", "--quiet", "--bare", "--initial-branch=main", str(self.origin))
        self.git(base, "init", "--quiet", "--initial-branch=main", str(self.clone))
        self.git(self.clone, "remote", "add", "origin", str(self.origin))
        self.commit_version("0.2.0.dev0", "start")
        self.git(self.clone, "push", "--quiet", "origin", "main")

    def git(self, folder, *args):
        return subprocess.run(["git", "-C", str(folder), *args], check=True, capture_output=True, text=True,
                              timeout=60).stdout

    def commit_version(self, version, message):
        init = self.clone / release.INIT
        init.parent.mkdir(parents=True, exist_ok=True)
        init.write_text(f'"""Package."""\n\n__version__ = "{version}"\n', encoding="utf-8")
        self.git(self.clone, "add", "--all")
        self.git(self.clone, "commit", "--quiet", "-m", message)

    def version_at(self, revision):
        source = self.git(self.clone, "show", f"{revision}:{release.INIT.as_posix()}")
        return re.search(r'^__version__ = "([^"]+)"$', source, re.MULTILINE).group(1)

    def run_release(self, version):
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            status = release.main([version], root=self.clone)
        return status, output.getvalue(), errors.getvalue()

    def assert_refused(self, version, reason):
        head, tags = self.git(self.clone, "rev-parse", "HEAD"), self.git(self.clone, "tag", "--list")
        status, output, errors = self.run_release(version)
        self.assertEqual((status, output), (1, ""))
        self.assertIn(reason, errors)
        self.assertEqual(self.git(self.clone, "rev-parse", "HEAD"), head)
        self.assertEqual(self.git(self.clone, "tag", "--list"), tags)

    def test_tags_the_release_and_starts_the_next_version(self):
        published = self.git(self.clone, "rev-parse", "HEAD")
        status, output, _ = self.run_release("0.2.0")
        self.assertEqual(status, 0)
        self.assertIn("git push origin main v0.2.0", output)
        self.assertIn("git switch --detach v0.2.0", output)
        self.assertEqual(self.git(self.clone, "log", "-2", "--format=%s").splitlines(),
                         ["chore: start 0.2.1.dev0", "chore: release 0.2.0"])
        self.assertEqual(self.git(self.clone, "cat-file", "-t", "v0.2.0").strip(), "tag")
        self.assertEqual(self.git(self.clone, "rev-parse", "v0.2.0^{commit}"),
                         self.git(self.clone, "rev-parse", "HEAD~1"))
        self.assertEqual((self.version_at("v0.2.0"), self.version_at("HEAD")), ("0.2.0", "0.2.1.dev0"))
        self.assertEqual(self.git(self.clone, "status", "--porcelain"), "")
        # Nothing is published.
        self.assertEqual(self.git(self.origin, "rev-parse", "main"), published)
        self.assertEqual(self.git(self.origin, "tag", "--list"), "")

    def test_a_release_may_skip_versions(self):
        self.assertEqual(self.run_release("0.3.0")[0], 0)
        self.assertEqual((self.version_at("v0.3.0"), self.version_at("HEAD")), ("0.3.0", "0.3.1.dev0"))

    def test_tags_a_version_already_set(self):
        self.commit_version("0.2.0", "set 0.2.0")
        self.git(self.clone, "push", "--quiet", "origin", "main")
        set_by_hand = self.git(self.clone, "rev-parse", "HEAD")
        self.assertEqual(self.run_release("0.2.0")[0], 0)
        self.assertEqual(self.git(self.clone, "rev-parse", "v0.2.0^{commit}"), set_by_hand)
        self.assertEqual(self.version_at("HEAD"), "0.2.1.dev0")

    def test_refuses_a_version_without_three_numbers(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
            release.main(["0.2"], root=self.clone)
        self.assertEqual(raised.exception.code, 2)

    def test_refuses_a_checkout_with_changes(self):
        (self.clone / "notes.txt").write_text("draft\n", encoding="utf-8")
        self.assert_refused("0.2.0", "the checkout has changes")

    def test_refuses_another_branch(self):
        self.git(self.clone, "switch", "--quiet", "--create", "feature")
        self.assert_refused("0.2.0", "releases are made on main, not on feature")

    def test_refuses_unpublished_commits(self):
        (self.clone / "notes.txt").write_text("draft\n", encoding="utf-8")
        self.git(self.clone, "add", "notes.txt")
        self.git(self.clone, "commit", "--quiet", "-m", "local change")
        self.assert_refused("0.2.0", "push or pull first")

    def test_refuses_an_existing_or_earlier_release(self):
        self.git(self.clone, "tag", "--annotate", "v0.2.0", "-m", "earlier")
        self.assert_refused("0.2.0", "the tag v0.2.0 exists already")
        self.git(self.clone, "tag", "--annotate", "v0.4.0", "-m", "later")
        self.assert_refused("0.3.0", "does not come after the release v0.4.0")
        self.assert_refused("0.1.9", "comes before the package's version, 0.2.0.dev0")


if __name__ == "__main__":
    unittest.main()
