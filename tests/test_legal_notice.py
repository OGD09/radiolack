"""Tests that the legal notice says the same everywhere.

The interface shows the notice in About's Legal notice dialog, and README.md
gives it to GitHub's readers: both are compared word for word, whatever their
markup (headings, lists, links and line breaks). The copyright's year and
holder are the same in the package's copyright file, README.md and About.
"""

import html.parser
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PAGE = ROOT / "src" / "radiolack" / "web" / "index.html"
README = ROOT / "README.md"
COPYRIGHT = ROOT / "packaging" / "copyright"
# Elements without an end tag, which must not count as a level deeper.
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}


class NoticeParser(html.parser.HTMLParser):
    """Collect the text inside the element whose id is legal-text."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.depth = 0
        self.found = False
        self.text: list[str] = []

    def handle_starttag(self, tag, attrs):
        if self.depth:
            self.depth += tag not in VOID
        elif dict(attrs).get("id") == "legal-text":
            self.depth = 1
            self.found = True

    def handle_endtag(self, tag):
        if self.depth and tag not in VOID:
            self.depth -= 1

    def handle_data(self, data):
        if self.depth:
            self.text.append(data)


def words(text: str) -> list[str]:
    """The words of ``text``, with their apostrophes; punctuation and markup aside."""
    return re.findall(r"[A-Za-z0-9]+(?:'[A-Za-z]+)?", text.replace("’", "'"))


def page_notice() -> str | None:
    """The interface's notice, or None without one."""
    parser = NoticeParser()
    parser.feed(PAGE.read_text(encoding="utf-8"))
    return " ".join(parser.text) if parser.found else None


def readme_notice() -> str | None:
    """README.md's Legal notice section, its links reduced to their text, or
    None without one."""
    match = re.search(r"^## Legal notice\n(.*?)^## ", README.read_text(encoding="utf-8"), re.MULTILINE | re.DOTALL)
    return re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", match.group(1)) if match else None


class LegalNoticeTest(unittest.TestCase):
    def test_readme_and_the_interface_say_the_same(self):
        page, readme = page_notice(), readme_notice()
        self.assertIsNotNone(page, "index.html has no element with id legal-text")
        self.assertIsNotNone(readme, "README.md has no Legal notice section")
        self.assertGreater(len(words(page)), 500)       # the whole notice, not an empty element
        self.assertEqual(words(readme), words(page))

    def test_every_copy_names_the_same_copyright_holder(self):
        match = re.search(r"^Copyright: (\d{4}) (\S.*)$", COPYRIGHT.read_text(encoding="utf-8"), re.MULTILINE)
        self.assertIsNotNone(match)
        year, holder = match.groups()
        page, readme = PAGE.read_text(encoding="utf-8"), README.read_text(encoding="utf-8")
        self.assertIn(f"© {year} {holder} · Free software", page)          # About
        self.assertIn(f"free software, copyright {year} {holder}.", page)  # the legal notice
        self.assertIn(f"free software, copyright {year} {holder}.", readme)
        self.assertIn(f"\nCopyright {year} {holder}\n", readme)            # the Licence section


if __name__ == "__main__":
    unittest.main()
