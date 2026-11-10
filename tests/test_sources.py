"""Tests for the sources Radiolack opens by kind. Nothing here reads a
source: nothing connects, and no rtl_tcp starts."""

import unittest

from radiolack.rtltcp import RtlSdrSource, RtlTcpSource
from radiolack.sources import Sources
from radiolack.synthetic import SyntheticBand


class SourcesTest(unittest.TestCase):
    def open(self, reopen):
        source = Sources().open(reopen)
        self.addCleanup(source.close)
        return source

    def test_opens_each_kind_the_simulated_band_first(self):
        self.assertEqual(Sources.kinds, ("synthetic", "rtl_sdr", "rtl_tcp"))
        self.assertIsInstance(self.open({"kind": "synthetic"}), SyntheticBand)
        rtl_sdr = self.open({"kind": "rtl_sdr"})
        self.assertIsInstance(rtl_sdr, RtlSdrSource)
        self.assertEqual(rtl_sdr.device, "0")      # the first receiver plugged in
        rtl_tcp = self.open({"kind": "rtl_tcp"})
        self.assertIs(type(rtl_tcp), RtlTcpSource)
        # rtl_tcp's default address, until the engine applies the saved one.
        self.assertEqual((rtl_tcp.address, rtl_tcp.given), ("127.0.0.1:1234", frozenset()))

    def test_a_source_opens_again_from_what_it_gives(self):
        for reopen in ({"kind": "synthetic"}, {"kind": "rtl_sdr", "device": "00000002"}, {"kind": "rtl_tcp"}):
            with self.subTest(reopen=reopen):
                source = self.open(reopen)
                self.assertEqual(source.reopen, reopen)
                again = self.open(source.reopen)
                self.assertEqual((again.kind, again.settings_key), (source.kind, source.settings_key))

    def test_refuses_an_unknown_kind_or_device(self):
        for reopen in ({}, {"kind": "soapy"}, {"kind": 3}, {"kind": "rtl_sdr", "device": ""},
                       {"kind": "rtl_sdr", "device": " "}, {"kind": "rtl_sdr", "device": 0},
                       {"kind": "rtl_sdr", "device": "x" * 65}):
            with self.subTest(reopen=reopen), self.assertRaises(ValueError):
                Sources().open(reopen)


if __name__ == "__main__":
    unittest.main()
