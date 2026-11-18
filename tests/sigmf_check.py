"""A check of the SigMF recordings that the tests write, against the rules
of SigMF 1.0.0 (https://sigmf.org) for the fields Radiolack writes, without
any dependency: the required objects and fields and their types, the
datatype's form, captures in the order of their samples within the data,
times in UTC, and every field outside the core namespace declared in
``core:extensions``."""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path
from typing import Any

# SigMF's datatypes: complex or real, then the type and its bits, little or
# big endian for those wider than a byte.
_DATATYPE = re.compile(r"(?P<kind>[cr])(?:(?P<float>f(?:32|64))|(?P<int>[iu](?:8|16|32)))(?P<order>_le|_be)?")
_DATETIME = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z")
_BITS = re.compile(r"\d+")


def sample_bytes(datatype: str) -> int:
    """The bytes of one sample, both components of a complex one, in ``datatype``."""
    match = _DATATYPE.fullmatch(datatype)
    if match is None:
        raise ValueError(f"not a SigMF datatype: {datatype}")
    bits = int(_BITS.search(datatype)[0])
    return bits // 8 * (2 if match["kind"] == "c" else 1)


def check_sigmf(test: unittest.TestCase, data: Path, meta: Path) -> dict[str, Any]:
    """Fail ``test`` unless ``data`` and ``meta`` make a SigMF recording as
    far as Radiolack writes one; return the metadata."""
    metadata = json.loads(meta.read_text(encoding="ascii"))
    test.assertIsInstance(metadata, dict)
    test.assertEqual(set(metadata), {"global", "captures", "annotations"})
    described = metadata["global"]
    test.assertIsInstance(described, dict)
    # Required: the datatype and the version.
    datatype = described["core:datatype"]
    match = _DATATYPE.fullmatch(datatype)
    test.assertIsNotNone(match, f"not a SigMF datatype: {datatype}")
    bits = int(_BITS.search(datatype)[0])
    test.assertEqual(match["order"] is not None, bits > 8, f"the byte order is given for wider types alone: {datatype}")
    test.assertEqual(described["core:version"], "1.0.0")
    rate = described["core:sample_rate"]
    test.assertTrue(isinstance(rate, (int, float)) and not isinstance(rate, bool) and rate > 0)
    for field in ("core:hw", "core:recorder"):
        test.assertIsInstance(described[field], str)
        test.assertTrue(described[field].isprintable())
    # Every namespace but the core declared, with what an extension object holds.
    extensions = described.get("core:extensions", [])
    test.assertIsInstance(extensions, list)
    for extension in extensions:
        test.assertEqual(set(extension), {"name", "version", "optional"})
        test.assertIsInstance(extension["name"], str)
        test.assertIsInstance(extension["version"], str)
        test.assertIsInstance(extension["optional"], bool)
    declared = {extension["name"] for extension in extensions}
    for name in described:
        namespace, _, field = name.partition(":")
        test.assertTrue(field, f"a field without its namespace: {name}")
        test.assertIn(namespace, {"core"} | declared, f"a namespace not declared: {name}")
    # The captures: each with its first sample, in order, within the data, its frequency and its time in UTC.
    size = data.stat().st_size
    test.assertEqual(size % sample_bytes(datatype), 0, "the data holds whole samples")
    samples = size // sample_bytes(datatype)
    captures = metadata["captures"]
    test.assertIsInstance(captures, list)
    test.assertTrue(captures, "a recording with samples has a capture")
    starts = []
    for capture in captures:
        start = capture["core:sample_start"]
        test.assertTrue(isinstance(start, int) and not isinstance(start, bool) and 0 <= start < samples)
        starts.append(start)
        frequency = capture["core:frequency"]
        test.assertTrue(isinstance(frequency, (int, float)) and not isinstance(frequency, bool))
        test.assertRegex(capture["core:datetime"], _DATETIME)
        test.assertLessEqual(set(capture), {"core:sample_start", "core:frequency", "core:datetime"})
    test.assertEqual(starts[0], 0)
    test.assertEqual(starts, sorted(set(starts)), "captures in the order of their samples, each with some")
    test.assertIsInstance(metadata["annotations"], list)
    return metadata
