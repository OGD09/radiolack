#!/usr/bin/env python3
"""Build Radiolack's logo files from the mark's geometry and the name's font.

The mark is the Peak: one spectrum trace rising out of the noise floor. In
its tile, blue on the interface's ink, it makes the icons: the favicon, the
iPhone's Home Screen icon, the menu and window icon, and GitHub's social
preview. Beside the name it takes a single colour, light on a dark
background and dark on a light one: on the pairing page and in
README.md's header. The name is Saira Italic at weight
600 and width 112.5, turned into outlines here once, so that neither the
interface nor the package loads a font. Saira is Copyright 2020 The Saira
Project Authors, under the SIL Open Font License 1.1.

Run by hand when the logo changes, with fontTools, Inkscape and ImageMagick
(convert):

    python3 packaging/identity.py --font Saira-Italic[wdth,wght].ttf

The font file is ofl/saira/Saira-Italic[wdth,wght].ttf from the google/fonts
repository at commit 3cfea32a77637ada0e209f10d9f91e7ff9d2a32f (Saira 1.101,
git blob 91a7f41356b3e936d3a0b7a9df53ce7a1fef0e3a); it stays out of the
repository. The script writes src/radiolack/web/favicon.svg and
apple-touch-icon.png, packaging/radiolack.svg, and in docs/identity/ the
README's radiolack-light.svg and radiolack-dark.svg and the social preview,
social-preview.png. It then prints the single-colour lockup that pair.html
inlines: tests/test_identity.py checks that it matches the README's.

It shapes the text as a browser does Latin text, which for Saira 1.101
means kerning alone: it stops if one of the font's other default features
would change the glyphs.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
WEB_DIR = ROOT / "src" / "radiolack" / "web"
DOCS_DIR = ROOT / "docs" / "identity"

NAME = "Radiolack"
TAGLINE = "Receive-only SDR receiver"
INK = "#0a0c0f"                 # the interface's background
SIGNAL = "#59a7ff"              # the interface's accent
LIGHT_THEME_TEXT = "#1f2328"    # GitHub's text colour, light theme
DARK_THEME_TEXT = "#e6edf3"     # GitHub's text colour, dark theme
SOCIAL_TEXT = "#e9edf2"
SOCIAL_MUTED = "#8b95a3"


@dataclass(frozen=True)
class Trace:
    """The Peak's trace in a 100 x 100 box, with the gradient under it."""

    path: str
    left: float       # where the trace starts, to close the fill under it
    width: float      # its stroke's width
    opacity: float    # the fill's opacity at its top
    bottom: float     # where the fill under the trace ends


# The icons' trace, and the favicon's, wider and bolder to hold at 16 pixels.
ICON_TRACE = Trace("M15 66 H33 C40 66 44.3 62.5 46 55 L50 22 L54 55 C55.7 62.5 60 66 67 66 H85", 15, 5.5, 0.34, 88)
FAVICON_TRACE = Trace("M9 65 H31 C38.5 65 43.2 61 45.2 52.5 L50 18 L54.8 52.5 C56.8 61 61.5 65 69 65 H91",
                      9, 9.5, 0.5, 100)
# The single-colour mark beside the name: the trace alone, without its fill.
LINE_TRACE = "M6 66 H29 C37 66 42 61.5 44.4 52 L50 12 L55.6 52 C58 61.5 63 66 71 66 H94"
LINE_WIDTH = 11
LINE_LEFT = 6 - LINE_WIDTH / 2      # the round cap where the floor starts
LINE_TOP = 12 - LINE_WIDTH / 2      # the round join above the peak
LINE_FLOOR = 66 + LINE_WIDTH / 2    # the floor's lower edge, which sits on the name's baseline

# The name, in font units (1000 to the em).
WEIGHT, WIDTH = 600, 112.5
TRACKING = 20           # between letters: 0.02 em
MARK_SCALE = 11.5       # font units to a unit of the mark's box: the mark is 1.15 em across
MARK_GAP = 460          # from the mark's box to the name
README_EM = 40          # the README's name, in pixels to the em
SOCIAL_EM, TAGLINE_EM = 120, 40

# What browsers apply to Latin text by default, besides kerning.
_SUBSTITUTIONS = ("rvrn", "ccmp", "locl", "liga", "clig", "calt")


def number(value: float) -> str:
    """``value`` with at most two decimals, as SVG writes it."""
    text = f"{value:.2f}".rstrip("0").rstrip(".")
    return "0" if text in ("-0", "") else text


def _svg(view_box: str, body: str, comment: str, width: float | None = None, height: float | None = None) -> str:
    size = f' width="{number(width)}" height="{number(height)}"' if width is not None and height is not None else ""
    return (f'<svg xmlns="http://www.w3.org/2000/svg"{size} viewBox="{view_box}">\n'
            f'  <!-- {comment} Built by packaging/identity.py. -->\n  {body}\n</svg>\n')


def _peak(trace: Trace, gradient: str) -> str:
    """The trace in the accent, over its gradient."""
    return (f'<defs><linearGradient id="{gradient}" x1="0" y1="0" x2="0" y2="1">'
            f'<stop offset=".2" stop-color="{SIGNAL}" stop-opacity="{number(trace.opacity)}"/>'
            f'<stop offset="1" stop-color="{SIGNAL}" stop-opacity="0"/></linearGradient></defs>'
            f'<path d="{trace.path} V{number(trace.bottom)} H{number(trace.left)} Z" fill="url(#{gradient})"/>'
            f'<path d="{trace.path}" fill="none" stroke="{SIGNAL}" stroke-width="{number(trace.width)}" '
            'stroke-linecap="round" stroke-linejoin="round"/>')


def _tile(gradient: str, clip: str) -> str:
    """The tile in a 100 x 100 box: the icon with a margin, and an edge in the accent."""
    return (f'<clipPath id="{clip}"><rect x="6" y="6" width="88" height="88" rx="19"/></clipPath>'
            f'<g clip-path="url(#{clip})"><rect x="6" y="6" width="88" height="88" fill="{INK}"/>'
            f'<g transform="translate(6 6) scale(.88)">{_peak(ICON_TRACE, gradient)}</g></g>'
            f'<rect x="6.5" y="6.5" width="87" height="87" rx="18.5" fill="none" stroke="{SIGNAL}" '
            'stroke-opacity=".32"/>')


def favicon_svg() -> str:
    """The favicon: the bolder trace on a tile that fills its square."""
    body = ('<clipPath id="tile"><rect width="100" height="100" rx="20"/></clipPath>'
            f'<g clip-path="url(#tile)"><rect width="100" height="100" fill="{INK}"/>'
            f'{_peak(FAVICON_TRACE, "fill")}</g>')
    return _svg("0 0 100 100", body, "Radiolack's favicon: the Peak on its tile.")


def touch_icon_svg() -> str:
    """The Home Screen icon's source: square and opaque, since iOS rounds and fills it itself."""
    body = f'<rect width="100" height="100" fill="{INK}"/>{_peak(ICON_TRACE, "fill")}'
    return _svg("0 0 100 100", body, "Radiolack's Home Screen icon: the Peak, full bleed.", 180, 180)


def desktop_icon_svg() -> str:
    """The menu and window icon."""
    return _svg("0 0 100 100", _tile("fill", "tile"), "Radiolack's icon: the Peak on its tile.", 128, 128)


# ------------------------------------------------------------------ the name


@dataclass(frozen=True)
class Outline:
    """Text drawn as one SVG path in font units, its baseline at y = 0 and y downwards."""

    path: str
    left: float
    top: float
    right: float
    bottom: float


def _instance(path: Path, weight: float, width: float):
    """Saira Italic's static instance at ``weight`` and ``width``."""
    try:
        from fontTools.ttLib import TTFont
        from fontTools.varLib import instancer
    except ImportError:
        sys.exit("identity.py needs fontTools (python3-fonttools on Debian)")
    try:
        font = TTFont(path)
    except OSError as error:
        sys.exit(f"identity.py: cannot read {path}: {error.strerror or error}")
    family = font["name"].getDebugName(1) or ""
    axes = {axis.axisTag for axis in font["fvar"].axes} if "fvar" in font else set()
    if not family.startswith("Saira") or font["post"].italicAngle == 0 or axes != {"wght", "wdth"}:
        sys.exit(f"identity.py: {path} is not Saira Italic's variable font")
    return instancer.instantiateVariableFont(font, {"wght": weight, "wdth": width})


def _lookups(font, tag: str, features: tuple[str, ...]):
    """(index, type, subtables) of each lookup that ``features``, or a required
    feature, apply to Latin text in the default language, in the order they apply."""
    if tag not in font:
        return
    table = font[tag].table
    scripts = {record.ScriptTag: record.Script for record in table.ScriptList.ScriptRecord}
    script = scripts.get("latn") or scripts.get("DFLT")
    if script is None or script.DefaultLangSys is None:
        return
    language, indices = script.DefaultLangSys, set()
    for index in language.FeatureIndex:
        record = table.FeatureList.FeatureRecord[index]
        if record.FeatureTag in features or index == language.ReqFeatureIndex:
            indices.update(record.Feature.LookupListIndex)
    extension = 7 if tag == "GSUB" else 9
    for index in sorted(indices):
        lookup = table.LookupList.Lookup[index]
        kind, subtables = lookup.LookupType, list(lookup.SubTable)
        if kind == extension:
            kind, subtables = subtables[0].ExtensionLookupType, [sub.ExtSubTable for sub in subtables]
        yield index, kind, subtables


def _context_at(glyphs: list[str], at: int, behind, inputs, ahead) -> bool:
    """Whether coverage-based context rules match ``glyphs`` at ``at``."""
    end = at + len(inputs)
    if at < len(behind) or end + len(ahead) > len(glyphs):
        return False
    return (all(glyphs[at + i] in coverage.glyphs for i, coverage in enumerate(inputs))
            and all(glyphs[at - 1 - i] in coverage.glyphs for i, coverage in enumerate(behind))
            and all(glyphs[end + i] in coverage.glyphs for i, coverage in enumerate(ahead)))


def _substitutes(kind: int, sub, glyphs: list[str]) -> bool:
    """Whether a substitution subtable could act on ``glyphs``: exactly for
    coverage-based context rules, conservatively for the others."""
    if kind in (5, 6) and sub.Format == 3:
        if kind == 5:
            return any(_context_at(glyphs, at, [], sub.Coverage, []) for at in range(len(glyphs)))
        return any(_context_at(glyphs, at, sub.BacktrackCoverage, sub.InputCoverage, sub.LookAheadCoverage)
                   for at in range(len(glyphs)))
    if kind in (1, 2):
        first = set(sub.mapping)
    elif kind == 3:
        first = set(sub.alternates)
    elif kind == 4:
        first = set(sub.ligatures)
    else:
        first = set(sub.Coverage.glyphs)
    return not first.isdisjoint(glyphs)


def _kerning(font, left: str, right: str) -> int:
    """The 'kern' feature's change to the advance between ``left`` and ``right``."""
    total = 0
    for index, kind, subtables in _lookups(font, "GPOS", ("kern",)):
        if kind != 2:
            sys.exit(f"identity.py: GPOS lookup {index} kerns in a way this script does not (type {kind})")
        for sub in subtables:
            if left not in sub.Coverage.glyphs:
                continue
            if sub.Format == 1:
                pairs = sub.PairSet[sub.Coverage.glyphs.index(left)].PairValueRecord
                record = next((pair for pair in pairs if pair.SecondGlyph == right), None)
                if record is None:
                    continue
            else:
                record = (sub.Class1Record[sub.ClassDef1.classDefs.get(left, 0)]
                          .Class2Record[sub.ClassDef2.classDefs.get(right, 0)])
            first, second = getattr(record, "Value1", None), getattr(record, "Value2", None)
            moves = [getattr(first, field, 0) for field in ("XPlacement", "YPlacement", "YAdvance")]
            moves += [getattr(second, field, 0) for field in ("XPlacement", "YPlacement", "XAdvance", "YAdvance")]
            if any(moves):
                sys.exit(f"identity.py: GPOS lookup {index} moves {left} {right} in a way this script does not")
            total += getattr(first, "XAdvance", 0) or 0
            break
    return total


def outline(font, text: str, x: float = 0.0, tracking: float = 0.0) -> Outline:
    """``text`` in ``font`` from ``x``, kerned, with ``tracking`` between letters."""
    from fontTools.pens.boundsPen import BoundsPen
    from fontTools.pens.svgPathPen import SVGPathPen
    from fontTools.pens.transformPen import TransformPen

    cmap = font.getBestCmap()
    missing = sorted({char for char in text if ord(char) not in cmap})
    if missing:
        sys.exit(f"identity.py: the font has no glyph for {''.join(missing)!r}")
    glyphs = [cmap[ord(char)] for char in text]
    for index, kind, subtables in _lookups(font, "GSUB", _SUBSTITUTIONS):
        if any(_substitutes(kind, sub, glyphs) for sub in subtables):
            sys.exit(f"identity.py: GSUB lookup {index} would change {text!r}: shaping it needs more than kerning")
    glyph_set, metrics = font.getGlyphSet(), font["hmtx"]
    # A tenth of a font unit is far below a pixel at any size the name is shown at.
    path, bounds = SVGPathPen(glyph_set, ntos=lambda value: number(round(value, 1))), BoundsPen(glyph_set)
    for i, name in enumerate(glyphs):
        flip = (1, 0, 0, -1, x, 0)
        glyph_set[name].draw(TransformPen(path, flip))
        glyph_set[name].draw(TransformPen(bounds, flip))
        x += metrics[name][0]
        if i + 1 < len(glyphs):
            x += _kerning(font, name, glyphs[i + 1]) + tracking
    left, top, right, bottom = bounds.bounds
    return Outline(path.getCommands(), left, top, right, bottom)


def lockup(word: Outline, colour: str) -> tuple[str, str]:
    """The mark beside the name, in ``colour``: the SVG body and its viewBox.

    ``word`` starts after the mark's box and its gap."""
    mark = (f'<path d="{LINE_TRACE}" transform="translate(0 {number(-LINE_FLOOR * MARK_SCALE)}) '
            f'scale({number(MARK_SCALE)})" fill="none" stroke="{colour}" stroke-width="{LINE_WIDTH}" '
            'stroke-linecap="round" stroke-linejoin="round"/>')
    name = f'<path d="{word.path}" fill="{colour}"/>'
    pad = 12
    left, right = LINE_LEFT * MARK_SCALE - pad, word.right + pad
    top, bottom = min(word.top, (LINE_TOP - LINE_FLOOR) * MARK_SCALE) - pad, max(word.bottom, 0) + pad
    return mark + name, " ".join(number(value) for value in (left, top, right - left, bottom - top))


def inline_lockup(word: Outline) -> str:
    """The lockup the pairing page inlines: the text's colour, sized by the style sheet."""
    body, view_box = lockup(word, "currentColor")
    return f'<svg role="img" aria-label="{NAME}" viewBox="{view_box}">{body}</svg>'


def readme_svg(word: Outline, colour: str, theme: str) -> str:
    """The README's header for one of GitHub's themes."""
    body, view_box = lockup(word, colour)
    width, height = (float(value) * README_EM / 1000 for value in view_box.split()[2:])
    return _svg(view_box, f"<title>{NAME}</title>{body}", f"Radiolack's name for GitHub's {theme} theme.",
                width, height)


def social_svg(word: Outline, tagline: Outline) -> str:
    """GitHub's social preview, 1280 x 640: the tile, the name and what Radiolack is."""
    tile, gap = 260, 64                         # the tile's box; its rounded square is 88 % of it
    name, small = SOCIAL_EM / 1000, TAGLINE_EM / 1000
    text_width = max((word.right - word.left) * name, (tagline.right - tagline.left) * small)
    seen = tile * 0.88
    x0 = (1280 - (seen + gap + text_width)) / 2 - tile * 0.06
    text_x = x0 + tile * 0.94 + gap
    line = 76                                   # from the name's baseline to the tagline's
    top, bottom = word.top * name, line + tagline.bottom * small
    baseline = 320 - (top + bottom) / 2
    body = (f'<rect width="1280" height="640" fill="{INK}"/>'
            f'<g transform="translate({number(x0)} {number(320 - tile / 2)}) scale({number(tile / 100)})">'
            f'{_tile("fill", "tile")}</g>'
            f'<path transform="translate({number(text_x - word.left * name)} {number(baseline)}) '
            f'scale({number(name)})" d="{word.path}" fill="{SOCIAL_TEXT}"/>'
            f'<path transform="translate({number(text_x - tagline.left * small)} {number(baseline + line)}) '
            f'scale({number(small)})" d="{tagline.path}" fill="{SOCIAL_MUTED}"/>')
    return _svg("0 0 1280 640", body, "GitHub's social preview for Radiolack.", 1280, 640)


def _png(svg: str, target: Path, width: int, height: int) -> None:
    """Draw ``svg`` with Inkscape into an opaque PNG without metadata."""
    inkscape, convert = shutil.which("inkscape"), shutil.which("convert")
    if inkscape is None or convert is None:
        sys.exit("identity.py needs Inkscape and ImageMagick's convert to draw the PNG files")
    with tempfile.TemporaryDirectory() as folder:
        source, drawn = Path(folder) / "source.svg", Path(folder) / "drawn.png"
        source.write_text(svg, "utf-8")
        for command in ([inkscape, "--export-type=png", f"--export-filename={drawn}", f"--export-width={width}",
                         f"--export-height={height}", str(source)],
                        [convert, str(drawn), "-alpha", "off", "-strip", f"PNG24:{target}"]):
            done = subprocess.run(command, capture_output=True, text=True, check=False)
            if done.returncode != 0:
                sys.exit(f"identity.py: {Path(command[0]).name} failed: {done.stderr.strip()}")
    print(f"wrote {target.relative_to(ROOT)}", file=sys.stderr)


def _write(target: Path, text: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, "utf-8")
    print(f"wrote {target.relative_to(ROOT)}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build Radiolack's logo files.")
    parser.add_argument("--font", type=Path, required=True,
                        help="Saira-Italic[wdth,wght].ttf from google/fonts (see this script's docstring)")
    args = parser.parse_args(argv)

    name_font = _instance(args.font, WEIGHT, WIDTH)
    word = outline(name_font, NAME, x=100 * MARK_SCALE + MARK_GAP, tracking=TRACKING)
    social_word = outline(name_font, NAME, tracking=TRACKING)
    tagline = outline(_instance(args.font, 400, 100), TAGLINE)

    _write(WEB_DIR / "favicon.svg", favicon_svg())
    _png(touch_icon_svg(), WEB_DIR / "apple-touch-icon.png", 180, 180)
    _write(HERE / "radiolack.svg", desktop_icon_svg())
    _write(DOCS_DIR / "radiolack-light.svg", readme_svg(word, LIGHT_THEME_TEXT, "light"))
    _write(DOCS_DIR / "radiolack-dark.svg", readme_svg(word, DARK_THEME_TEXT, "dark"))
    _png(social_svg(social_word, tagline), DOCS_DIR / "social-preview.png", 1280, 640)
    print(inline_lockup(word))
    return 0


if __name__ == "__main__":
    sys.exit(main())
