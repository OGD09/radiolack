"""Tests for Radiolack's logo and icons, which packaging/identity.py draws:
the copies in the interface's pages, the package and README.md agree with
one another and with the script, and none needs a font or another file.
Drawing them again needs the font, which stays out of the repository: these
tests read the files the script wrote."""

import importlib.util
import re
import struct
import sys
import unittest
import xml.etree.ElementTree as ElementTree
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "src" / "radiolack" / "web"
DOCS = ROOT / "docs" / "identity"
spec = importlib.util.spec_from_file_location("identity", ROOT / "packaging" / "identity.py")
identity = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = identity      # where its dataclasses look their module up
spec.loader.exec_module(identity)

# The logo that a page inlines in its heading.
INLINE_LOGO = re.compile(r"<h1\b[^>]*>(<svg\b.*?</svg>)</h1>", re.DOTALL)
# What would load a font, a style, a script or another file, or draw text with a font.
FORBIDDEN_ELEMENTS = {"text", "tspan", "textPath", "font", "font-face", "style", "script", "image", "use",
                      "foreignObject", "a"}
FORBIDDEN_ATTRIBUTES = {"style", "href", "font", "font-family"}
LOCAL_REFERENCE = re.compile(r"url\(#([A-Za-z][\w-]*)\)")


def local_name(name: str) -> str:
    """A tag or attribute name without its XML namespace."""
    return name.rsplit("}", 1)[-1]


def inline_logo(page: str) -> str:
    """The logo that the interface's page ``page`` inlines."""
    found = INLINE_LOGO.findall((WEB / page).read_text(encoding="utf-8"))
    if len(found) != 1:
        raise AssertionError(f"{page} inlines {len(found)} logos")
    return found[0]


def png_chunks(path: Path) -> list[tuple[str, bytes]]:
    """The chunks of the PNG file at ``path``, in order: their types and data."""
    data = path.read_bytes()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise AssertionError(f"{path.name} is not a PNG file")
    chunks, at = [], 8
    while at < len(data):
        length, kind = struct.unpack(">I4s", data[at:at + 8])
        chunks.append((kind.decode("ascii"), data[at + 8:at + 8 + length]))
        at += 12 + length
    return chunks


class IconTest(unittest.TestCase):
    def test_the_svg_icons_are_the_ones_the_script_draws(self):
        self.assertEqual((WEB / "favicon.svg").read_text(encoding="utf-8"), identity.favicon_svg())
        self.assertEqual((ROOT / "packaging" / "radiolack.svg").read_text(encoding="utf-8"),
                         identity.desktop_icon_svg())

    def test_the_png_files_are_opaque_without_metadata(self):
        # iOS rounds the Home Screen icon's corners and fills its transparency with black: the icon fills its
        # square with the tile's ink. GitHub takes a social preview of 1280 x 640 under 1 MB.
        ink = bytes.fromhex(identity.INK.removeprefix("#"))
        for path, size in ((WEB / "apple-touch-icon.png", (180, 180)), (DOCS / "social-preview.png", (1280, 640))):
            with self.subTest(file=path.name):
                chunks = png_chunks(path)
                self.assertEqual({kind for kind, _ in chunks}, {"IHDR", "IDAT", "IEND"})
                width, height, depth, colour, _, _, interlace = struct.unpack(">IIBBBBB", chunks[0][1])
                self.assertEqual(((width, height), depth, colour, interlace), (size, 8, 2, 0))   # RGB, no alpha
                pixels = zlib.decompress(b"".join(data for kind, data in chunks if kind == "IDAT"))
                # Each row starts with its filter's type; whatever the filter, the first pixel is stored as is.
                self.assertEqual(pixels[1:4], ink)
                self.assertLess(path.stat().st_size, 1_000_000)


class LogoTest(unittest.TestCase):
    def logos(self) -> dict[str, ElementTree.Element]:
        """Every copy of the mark and the name together, by where it is."""
        copies = {page: ElementTree.fromstring(inline_logo(page)) for page in ("index.html", "pair.html")}
        for theme in ("light", "dark"):
            copies[f"radiolack-{theme}.svg"] = ElementTree.parse(DOCS / f"radiolack-{theme}.svg").getroot()
        return copies

    def test_the_pages_inline_the_same_logo_for_assistive_technologies(self):
        index = (WEB / "index.html").read_text(encoding="utf-8")
        self.assertIn('<h1 id="brand"><svg ', index)
        self.assertEqual(inline_logo("index.html"), inline_logo("pair.html"))
        logo = ElementTree.fromstring(inline_logo("index.html"))
        self.assertEqual((logo.get("role"), logo.get("aria-label")), ("img", "Radiolack"))
        # The style sheets size it: neither a width nor a height.
        self.assertEqual({"width", "height"} & set(logo.attrib), set())

    def test_every_copy_is_the_same_drawing_in_its_own_colour(self):
        colours = {"index.html": "currentColor", "pair.html": "currentColor",
                   "radiolack-light.svg": identity.LIGHT_THEME_TEXT, "radiolack-dark.svg": identity.DARK_THEME_TEXT}
        drawings = {}
        for where, logo in self.logos().items():
            paths = []
            for path in logo.iter():
                if local_name(path.tag) != "path":
                    continue
                attributes = dict(path.attrib)
                for paint in ("fill", "stroke"):
                    if paint in attributes and attributes[paint] != "none":
                        self.assertEqual(attributes[paint], colours[where], f"{where}: {paint}")
                        attributes[paint] = "text"
                paths.append(attributes)
            drawings[where] = (logo.get("viewBox"), paths)
        self.assertEqual(len(drawings["index.html"][1]), 2)    # the mark, then the name
        for where, drawing in drawings.items():
            with self.subTest(copy=where):
                self.assertEqual(drawing, drawings["index.html"])

    def test_the_readme_files_name_radiolack(self):
        for theme in ("light", "dark"):
            with self.subTest(theme=theme):
                logo = ElementTree.parse(DOCS / f"radiolack-{theme}.svg").getroot()
                [title] = [element for element in logo if local_name(element.tag) == "title"]
                self.assertEqual(title.text, "Radiolack")

    def test_the_readme_shows_the_logo_of_the_readers_theme(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        header = readme[:readme.index("</h1>")]
        self.assertTrue(readme.startswith("<h1>\n  <picture>\n"))
        self.assertIn('<source media="(prefers-color-scheme: dark)" srcset="docs/identity/radiolack-dark.svg">',
                      header)
        self.assertIn('<img src="docs/identity/radiolack-light.svg" alt="Radiolack">', header)

    def test_no_copy_needs_a_font_or_another_file(self):
        copies = dict(self.logos())
        copies["favicon.svg"] = ElementTree.parse(WEB / "favicon.svg").getroot()
        copies["radiolack.svg"] = ElementTree.parse(ROOT / "packaging" / "radiolack.svg").getroot()
        for where, logo in copies.items():
            with self.subTest(copy=where):
                ids = {element.get("id") for element in logo.iter()} - {None}
                for element in logo.iter():
                    self.assertNotIn(local_name(element.tag), FORBIDDEN_ELEMENTS)
                    for name, value in element.attrib.items():
                        self.assertNotIn(local_name(name), FORBIDDEN_ATTRIBUTES)
                        self.assertFalse(local_name(name).startswith("on"), name)
                        if "url(" in value:
                            reference = LOCAL_REFERENCE.fullmatch(value)
                            self.assertIsNotNone(reference, value)
                            self.assertIn(reference.group(1), ids)


if __name__ == "__main__":
    unittest.main()
