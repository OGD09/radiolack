"""Tests for the problem reports: what they give, what they leave out, and
GitHub's page for a new issue that carries them."""

import io
import logging
import re
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from radiolack import __version__, report

LINE = re.compile(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\dZ (WARNING|ERROR) radiolack\.tests: .+$")


class RecentMessagesTest(unittest.TestCase):
    """The latest warnings and errors, kept in memory, one line each."""

    def setUp(self):
        self.recent = report.RecentMessages(size=3)
        self.logger = logging.getLogger("radiolack.tests")
        self.logger.addHandler(self.recent)
        self.addCleanup(self.logger.removeHandler, self.recent)
        self.logger.propagate = False   # nothing on the test run's error output
        self.addCleanup(setattr, self.logger, "propagate", True)

    def test_keeps_the_latest_warnings_and_errors(self):
        self.logger.info("tuned")
        for message in ("first", "second", "third", "fourth"):
            self.logger.warning("%s warning", message)
        self.logger.error("an error")
        lines = self.recent.lines()
        self.assertEqual([line.split(": ", 1)[1] for line in lines], ["third warning", "fourth warning", "an error"])
        for line in lines:
            self.assertRegex(line, LINE)

    def test_writes_each_on_one_line_in_utc(self):
        record = logging.LogRecord("radiolack.engine", logging.WARNING, __file__, 1, "two\nlines  apart", (), None)
        record.created = 0.0
        self.recent.handle(record)
        self.assertEqual(self.recent.lines(), ["1970-01-01 00:00:00Z WARNING radiolack.engine: two lines apart"])

    def test_names_an_exception_by_its_type_and_message(self):
        try:
            raise OSError("No space left on device")
        except OSError:
            self.logger.exception("the settings were not saved")
        [line] = self.recent.lines()
        self.assertTrue(line.endswith("ERROR radiolack.tests: the settings were not saved "
                                      "(OSError: No space left on device)"), line)

    def test_cuts_a_long_message(self):
        self.logger.warning("x" * 2000)
        [line] = self.recent.lines()
        self.assertEqual(len(line), report.MAX_MESSAGE)
        self.assertTrue(line.endswith("x..."))


class KeepRecentMessagesTest(unittest.TestCase):
    def setUp(self):
        # As at a start: no handler yet, which a run of cli.main() in another test may have added.
        logger = logging.getLogger(report.LOGGER)
        self.addCleanup(setattr, logger, "handlers", logger.handlers)
        logger.handlers = []
        self.enterContext(mock.patch.object(report, "_recent", None))

    def test_keeps_them_once_and_still_writes_them_on_the_error_output(self):
        recent = report.keep_recent_messages()
        self.assertIs(report.keep_recent_messages(), recent)
        output = io.StringIO()
        with redirect_stderr(output):
            logging.getLogger("radiolack.engine").warning("the disk is full")
            logging.getLogger("radiolack.engine").info("tuned")
        self.assertEqual(output.getvalue(), "the disk is full\n")       # as Python writes it without a handler
        self.assertTrue(recent.lines()[-1].endswith(" WARNING radiolack.engine: the disk is full"))


class ReportTest(unittest.TestCase):
    RECEIVER = "rtl_tcp, streaming, tuner R820T, 2.048 MS/s"

    def setUp(self):
        self.recent = report.RecentMessages()
        self.enterContext(mock.patch.object(report, "_recent", self.recent))
        self.enterContext(mock.patch.object(report, "_window", None))
        self.enterContext(mock.patch.object(report.Path, "home", return_value=Path("/home/someone")))

    def warn(self, message: str) -> None:
        record = logging.LogRecord("radiolack.store", logging.WARNING, __file__, 1, message, (), None)
        record.created = 0.0
        self.recent.handle(record)

    def test_gives_what_radiolack_runs_on_and_its_latest_messages(self):
        report.set_window("GTK 3.24.38, WebKitGTK 2.50.6")
        self.warn("/home/someone/.config/radiolack/settings.json could not be read: kept aside")
        lines = report.Report.now(self.RECEIVER, "a browser; WebGL2 views").text().splitlines()
        self.assertTrue(lines[0].startswith(f"Radiolack {__version__}"), lines[0])
        self.assertTrue(lines[1].startswith("Python "), lines[1])
        self.assertIn("aiohttp ", lines[1])
        self.assertTrue(lines[2].startswith("System: "), lines[2])
        self.assertEqual(lines[3:6], ["Window: GTK 3.24.38, WebKitGTK 2.50.6", "Interface: a browser; WebGL2 views",
                                      f"Receiver: {self.RECEIVER}"])
        self.assertTrue(lines[6].startswith("Running for "), lines[6])
        # The home folder is "~".
        self.assertEqual(lines[7:], ["", "Latest warnings and errors (UTC):",
                                     "1970-01-01 00:00:00Z WARNING radiolack.store: "
                                     "~/.config/radiolack/settings.json could not be read: kept aside"])

    def test_without_a_window_page_or_message(self):
        text = report.Report.now(self.RECEIVER).text()
        self.assertNotIn("Window:", text)
        self.assertNotIn("Interface:", text)
        self.assertTrue(text.endswith("\n\nNo warnings or errors since Radiolack started."), text)
        with mock.patch.object(report, "_recent", None):
            self.assertTrue(report.Report.now(self.RECEIVER).text().endswith("\n\nWarnings and errors: not kept."))

    def test_opens_github_with_the_report_under_a_heading_for_the_problem(self):
        self.warn("the recording stopped: the disk is full")
        made = report.Report.now(self.RECEIVER)
        url = made.issue_url()
        self.assertTrue(url.startswith(f"{report.ISSUES_URL}?"), url)
        [body] = parse_qs(urlsplit(url).query)["body"]
        self.assertTrue(body.startswith("**What happened**\n\n"), body)
        self.assertTrue(body.endswith(f"**Report from Radiolack**\n\n```text\n{made.text()}\n```\n"), body)

    def test_leaves_the_oldest_messages_out_of_the_issue_to_keep_its_address_short(self):
        messages = tuple(f"2026-11-15 12:{n:02d}:00Z WARNING radiolack.engine: {n} " + "x" * 300 for n in range(50))
        made = report.Report(("Radiolack 9.9.9",), messages)
        url = made.issue_url()
        self.assertLessEqual(len(url), report.MAX_ISSUE_URL)
        [body] = parse_qs(urlsplit(url).query)["body"]
        left_out = int(re.search(r"\((\d+) older left out: copy the report in Radiolack to give them all\)",
                                 body)[1])
        self.assertGreater(left_out, 0)
        self.assertNotIn(messages[left_out - 1], body)
        self.assertTrue(all(message in body for message in messages[left_out:]))
        self.assertTrue(all(message in made.text() for message in messages))     # what Copy gives

    def test_leaves_a_home_folder_of_slash_alone(self):
        with mock.patch.object(report.Path, "home", return_value=Path("/")):
            self.assertEqual(report._home_hidden("/var/lib/radiolack"), "/var/lib/radiolack")

    def test_how_long_radiolack_has_run(self):
        for seconds, shown in ((59, "less than a minute"), (60, "1 min"), (110 * 60, "1 h 50 min"),
                               (86_400 + 120, "1 d 2 min"), (2 * 86_400, "2 d")):
            with self.subTest(seconds=seconds):
                self.assertEqual(report._duration(seconds), shown)


class ReceiverLineTest(unittest.TestCase):
    def test_kind_state_tuner_and_rate_without_address_or_frequency(self):
        state = {"kind": "rtl_tcp", "status": "waiting", "detail": "no answer", "address": "192.0.2.7:1234",
                 "tuner": "R820T", "centre_hz": 145_500_000, "sample_rate_hz": 2_048_000}
        self.assertEqual(report.receiver_line(state), "rtl_tcp, waiting, tuner R820T, 2.048 MS/s (no answer)")
        state = {"kind": "synthetic", "status": "streaming", "detail": "", "address": None, "tuner": None,
                 "centre_hz": 145_000_000, "sample_rate_hz": 1_024_000}
        self.assertEqual(report.receiver_line(state), "synthetic, streaming, 1.024 MS/s")


class CheckPageTest(unittest.TestCase):
    def test_takes_a_line_of_text(self):
        for page in ("", "a browser; WebGL2 views; 1280x800 at 1x", "x" * report.MAX_PAGE):
            with self.subTest(page=page[:20]):
                self.assertEqual(report.check_page(page), page)

    def test_refuses_anything_else(self):
        for page in ("x" * (report.MAX_PAGE + 1), "two\nlines", "\x1b[31m", 3, None, ["page"]):
            with self.subTest(page=page if not isinstance(page, str) else page[:20]), \
                    self.assertRaisesRegex(ValueError, "^page must be a line of text of at most 300 characters$"):
                report.check_page(page)


if __name__ == "__main__":
    unittest.main()
