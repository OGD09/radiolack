"""Tests for the rtl_tcp sources, against a fake rtl_tcp server on an
ephemeral port, and a fake rtl_tcp program that never opens a receiver."""

import asyncio
import functools
import logging
import os
import shutil
import sys
import time
import unittest
from pathlib import Path

import numpy as np

from radiolack.rtltcp import RtlSdrSource, RtlTcpSource, parse_address, tuner_for
from radiolack import r82xx
from tests.fakes import FAKE_RTL_TCP, LOOPBACK, FakeRtlTcp, FakeRtlTcpRuns, fake_codes, free_port, sample_codes

FAST = {"settle_s": 0.15, "idle_s": 1.0, "read_wait_s": 0.2, "timeout_s": 0.5, "retry_s": (0.05, 0.2)}
COUNT = 400         # samples per read: less than one chunk of the fake server
# Stands in for Radiolack: runs the fake rtl_tcp program given as arguments,
# for the device given first, through an RtlSdrSource, says when samples
# arrive, then waits to be killed.
KILLED_RADIOLACK = """
import asyncio, sys
from radiolack.rtltcp import RtlSdrSource

async def main():
    source = RtlSdrSource(sys.argv[1], program=sys.argv[2:], idle_s=60)
    while await source.read(1000) is None:
        pass
    print("streaming", flush=True)
    await asyncio.sleep(60)

asyncio.run(main())
"""


# Commands sent at each connection with automatic gain: the gain mode only.
CONNECTION = 7


def connection_commands(ppm: int, rate_hz: int, centre_hz: int, gain_tenths: int | None = None,
                        direct: int = 0, rtl_agc: int = 0, bias_tee: int = 0) -> list[tuple[int, int]]:
    """What the source sends at each connection: the correction, direct
    sampling, the RTL2832's AGC, the frequency, the sample rate, the gain,
    unless sampling directly, then the antenna power. The sample rate and
    the gain follow 0x09 with 0, which starts the tuner again."""
    commands = [(0x05, ppm % 2**32), (0x09, direct), (0x08, rtl_agc), (0x01, centre_hz), (0x02, rate_hz)]
    if not direct:
        commands += [(0x03, 0)] if gain_tenths is None else [(0x03, 1), (0x04, gain_tenths)]
    return [*commands, (0x0e, bias_tee)]


async def eventually(condition, timeout: float = 5.0) -> None:
    """Wait until ``condition()`` holds."""
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("the condition never held")
        await asyncio.sleep(0.01)


def strongest_hz(samples: np.ndarray, rate_hz: float) -> float:
    """The frequency of the strongest line in ``samples``, from the centre."""
    spectrum = np.abs(np.fft.fftshift(np.fft.fft(samples * np.hanning(len(samples)))))
    return (int(np.argmax(spectrum)) - len(samples) // 2) * rate_hz / len(samples)


def carrier_hz(samples: np.ndarray, rate_hz: float, near_hz: float) -> float:
    """The frequency of a lone carrier near ``near_hz`` from the centre, to
    a small fraction of a hertz: mixed down by ``near_hz``, averaged over
    blocks of 64 samples, then the slope of its phase."""
    block = 64
    count = len(samples) // block * block
    mixed = samples[:count] * np.exp(-2j * np.pi * near_hz * np.arange(count) / rate_hz)
    phase = np.unwrap(np.angle(mixed.reshape(-1, block).mean(axis=1)))
    slope = np.polyfit(np.arange(len(phase)) * block / rate_hz, phase, 1)[0]
    return near_hz + slope / (2 * np.pi)


def running(pid: int) -> bool:
    """Whether a process runs, an ended one waiting for its parent aside (Linux)."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except (FileNotFoundError, ProcessLookupError):
        return False
    return stat.rsplit(")", 1)[1].split()[0] not in ("Z", "X")


class SourceTest(unittest.IsolatedAsyncioTestCase):
    """One source on one fake server; each test closes its own server."""

    async def start(self, fake: FakeRtlTcp | None = None, *, grid: bool = False, **options) -> RtlTcpSource:
        """A source on ``fake``, an R820T unless given another. The tests of
        the connection, the settings and the correction ask the frequencies
        as they are: off the R820T's grid unless ``grid``, which GridTest
        tests."""
        self.fake = fake if fake is not None else FakeRtlTcp()
        await self.fake.__aenter__()
        self.addAsyncCleanup(self.fake.__aexit__, None, None, None)
        source = RtlTcpSource(LOOPBACK, self.fake.port, **{**FAST, **options})
        if not grid:
            source.configure({"tuner_grid": False})
        self.addCleanup(source.close)
        self.changes = 0

        def changed():
            self.changes += 1

        source.on_change = changed
        return source

    async def read(self, source: RtlTcpSource, count: int = COUNT, timeout: float = 5.0) -> np.ndarray:
        """The next samples, reading again while the source waits."""
        deadline = time.monotonic() + timeout
        while (samples := await source.read(count)) is None:
            if time.monotonic() > deadline:
                raise AssertionError(f"no samples: {source.status} ({source.detail})")
        self.assertEqual(len(samples), count)
        self.assertEqual(samples.dtype, np.complex64)
        return samples

    def assert_labelled(self, source: RtlTcpSource, samples: np.ndarray) -> None:
        """The samples were taken with the setting the source gives them:
        the centre in use, which the source labels them with."""
        i_codes, q_codes = sample_codes(samples)
        i_code, q_code = fake_codes(round(source.exact_centre_hz), source.sample_rate_hz)
        self.assertEqual((i_codes, q_codes), ({i_code}, {q_code}))


class StreamingTest(SourceTest):
    async def test_sends_its_settings_then_labels_the_samples_with_them(self):
        source = await self.start(centre_hz=433_000_000, sample_rate_hz=1_024_000)
        self.assertEqual(source.status, "idle")
        for _ in range(5):
            self.assert_labelled(source, await self.read(source))
        self.assertEqual((source.centre_hz, source.sample_rate_hz), (433e6, 1_024_000.0))
        self.assertEqual(self.fake.commands, connection_commands(0, 1_024_000, 433_000_000))
        self.assertEqual(source.status, "streaming")

    async def test_converts_unsigned_bytes_to_full_scale(self):
        source = await self.start()
        samples = await self.read(source)
        i_code, q_code = fake_codes(145e6, 2.4e6)
        expected = complex((i_code - 127.5) / 127.5, (q_code - 127.5) / 127.5)
        np.testing.assert_allclose(samples, np.full(COUNT, expected), rtol=0, atol=1e-6)

    async def test_describes_the_receiver(self):
        source = await self.start(gain_db=30)
        self.assertEqual(source.describe()["tuner"], None)
        await self.read(source)
        state = source.describe()
        self.assertEqual(state, {
            "kind": "rtl_tcp", "status": "streaming", "detail": "",
            "address": f"{LOOPBACK}:{self.fake.port}", "tuner": "Rafael Micro R820T", "driver": None,
            "centre_hz": 145e6, "centre_in_use_hz": 145e6, "sample_rate_hz": 2.4e6,
            "sample_rates_hz": [240e3, 300e3, 1.024e6, 1.536e6, 2.048e6, 2.4e6, 2.88e6], "gain_db": 29.7, "ppm": 0,
            "gains_db": state["gains_db"], "range_hz": [24e6, 1766e6],
            "hf_input": "none", "hf_inputs": ["none", "q", "i", "tuner"], "direct": False,
            "rtl_agc": False, "bias_tee": False, "converter": "none", "converter_lo_hz": 125e6,
            "driver_pll": "unknown", "tuner_grid": False,
            "settable": ["address", "hf_input", "converter_lo_hz", "converter", "sample_rate_hz", "centre_hz",
                         "gain_db", "ppm", "driver_pll", "tuner_grid", "rtl_agc", "bias_tee"],
        })
        # One set of settings whatever the address, which was given here.
        self.assertEqual((source.settings_key, source.given), ("rtl_tcp", frozenset({"address"})))
        self.assertEqual(len(state["gains_db"]), 29)
        self.assertEqual(self.fake.commands, connection_commands(0, 2_400_000, 145_000_000, 297))
        self.assertGreaterEqual(self.changes, 2)    # connecting, then streaming

    async def test_a_reader_that_falls_behind_gets_the_latest_samples(self):
        source = await self.start(buffer_s=0.01)
        await self.read(source)
        self.fake.frequency_hz = 433_000_000    # what the server sends from now on
        await asyncio.sleep(0.4)                # far more than the source keeps
        i_codes, _ = sample_codes(await self.read(source))
        self.assertEqual(i_codes, {fake_codes(433e6, 0)[0]})

    async def test_logs_the_samples_dropped_for_a_reader_behind_without_the_address(self):
        source = await self.start(buffer_s=0.01)
        with self.assertLogs("radiolack.rtltcp", logging.WARNING) as logs:
            await self.read(source)
            await asyncio.sleep(0.3)            # far more than the source keeps
            await self.read(source)
        [message] = [record.getMessage() for record in logs.records]
        self.assertRegex(message, r"^rtl_tcp: [1-9]\d* samples dropped \(\d+\.\d s\), the engine behind, "
                                  r"in the last \d+ s$")
        self.assertNotIn(str(self.fake.port), message)
        # At most once a minute: the drops that follow wait for the next line.
        await asyncio.sleep(0.3)
        with self.assertNoLogs("radiolack.rtltcp", logging.WARNING):
            await self.read(source)
        with self.assertLogs("radiolack.rtltcp", logging.WARNING) as logs:
            source.close()
            await source.wait_closed()          # its last count goes out as it stops
        self.assertRegex(logs.records[-1].getMessage(), r"^rtl_tcp: [1-9]\d* samples dropped \(\d+\.\d s\)")


class SettingsTest(SourceTest):
    async def test_retunes_and_drops_the_samples_of_the_previous_setting(self):
        source = await self.start()
        await self.read(source)
        source.configure({"centre_hz": 433_000_000})
        await eventually(lambda: self.fake.commands[-1] == (0x01, 433_000_000))
        for _ in range(5):
            self.assert_labelled(source, await self.read(source))
        self.assertEqual(source.centre_hz, 433e6)

    async def test_without_settling_the_old_samples_would_be_mislabelled(self):
        # The fake server keeps sending the old setting for a while, as a
        # receiver does: the settling time is what keeps the labels right.
        source = await self.start(settle_s=0.0)
        await self.read(source)
        await eventually(lambda: self.fake.frequency_hz == 145_000_000)
        source.configure({"centre_hz": 433_000_000})
        i_codes, _ = sample_codes(await self.read(source))
        self.assertEqual(source.centre_hz, 433e6)
        self.assertIn(fake_codes(145e6, 0)[0], i_codes)

    async def test_a_new_sample_rate_also_sends_the_frequency(self):
        source = await self.start()
        await self.read(source)
        source.configure({"sample_rate_hz": 1_024_000})
        await eventually(lambda: len(self.fake.commands) == CONNECTION + 2)
        self.assertEqual(self.fake.commands[CONNECTION:], [(0x02, 1_024_000), (0x01, 145_000_000)])
        self.assert_labelled(source, await self.read(source))
        self.assertEqual(source.sample_rate_hz, 1_024_000.0)

    async def test_takes_librtlsdr_s_lower_range_of_sample_rates(self):
        source = await self.start()
        await self.read(source)
        for rate in (225_001, 240_000, 300_000, 900_001):
            with self.subTest(rate=rate):
                source.configure({"sample_rate_hz": rate})
                await eventually(lambda rate=rate: self.fake.commands[-2:] == [(0x02, rate), (0x01, 145_000_000)])
                self.assert_labelled(source, await self.read(source))
                self.assertEqual(source.sample_rate_hz, float(rate))
        with self.assertRaisesRegex(ValueError, "^the sample rate must be from 0.225001 to 0.3 "
                                                "or from 0.900001 to 3.2 MS/s$"):
            source.configure({"sample_rate_hz": 600_000})
        # At 240 kS/s, direct sampling's span starts at 0 Hz from a centre of 120 kHz.
        source.configure({"sample_rate_hz": 240_000, "hf_input": "q", "centre_hz": 120_000})
        self.assertEqual(source.describe()["range_hz"], [120e3, 1766e6])

    async def test_gain_snaps_to_the_tuner_and_none_is_automatic(self):
        source = await self.start()
        await self.read(source)
        source.configure({"gain_db": 30})
        source.configure({"gain_db": -5})
        source.configure({"gain_db": None})
        await eventually(lambda: len(self.fake.commands) == CONNECTION + 5)
        self.assertEqual(self.fake.commands[CONNECTION:],
                         [(0x03, 1), (0x04, 297), (0x03, 1), (0x04, 0), (0x03, 0)])
        self.assertIsNone(source.describe()["gain_db"])

    async def test_negative_gains_go_in_twos_complement(self):
        source = await self.start(FakeRtlTcp(tuner_type=1, gain_count=14))
        await self.read(source)
        source.configure({"gain_db": -1})
        await eventually(lambda: self.fake.commands[-1:] == [(0x04, 2**32 - 10)])
        self.assertEqual(source.describe()["gain_db"], -1.0)

    async def test_a_frequency_correction_retunes_and_drops_the_samples_on_their_way(self):
        # An E4000 takes the whole part itself (an R820T's tests: GridTest).
        source = await self.start(FakeRtlTcp(tuner_type=1, gain_count=14), ppm=5)
        await self.read(source)
        self.assertEqual(self.fake.commands, connection_commands(5, 2_400_000, 145_000_000))
        changed = time.monotonic()
        source.configure({"ppm": -2.6})     # -3 by the receiver, the rest by the frequency asked
        await eventually(lambda: len(self.fake.commands) == CONNECTION + 2)
        self.assertEqual(self.fake.commands[CONNECTION:], [(0x05, 2**32 - 3), (0x01, 144_999_942)])
        self.assertEqual(source.describe()["ppm"], -2.6)
        await self.read(source)
        self.assertGreaterEqual(time.monotonic() - changed, FAST["settle_s"])

    async def test_refuses_invalid_changes_and_changes_nothing(self):
        source = await self.start()
        await self.read(source)
        before, commands, changes = source.describe(), list(self.fake.commands), self.changes
        refused = [
            {"frequency": 145e6},
            {"centre_hz": 145e6, "volume": 3},
            {"centre_hz": "145000000"},
            {"centre_hz": True},
            {"centre_hz": float("nan")},
            {"centre_hz": float("inf")},
            {"centre_hz": 10**400},
            {"centre_hz": 20e6},
            {"centre_hz": 1800e6},
            {"sample_rate_hz": 225_000},
            {"sample_rate_hz": 300_001},
            {"sample_rate_hz": 900_000},
            {"sample_rate_hz": 3_300_000},
            {"gain_db": "auto"},
            {"ppm": 101},
            {"ppm": 100.01},
            {"ppm": -100.6},
            {"ppm": "5"},
            {"ppm": None},
            {"ppm": True},
            {"ppm": float("nan")},
            {"centre_hz": 433e6, "sample_rate_hz": 100},
            {"centre_hz": 433e6, "ppm": 200},
            {"address": ""},
            {"address": None},
            {"address": 1234},
            {"address": "host:port"},
            {"address": "::1"},
            {"address": "192.0.2.1 1234"},
            {"address": "192.0.2.1:65536"},
            {"address": "h" * 254},
            {"address": "192.0.2.1", "ppm": 200},
        ]
        for change in refused:
            with self.subTest(change=change), self.assertRaises(ValueError):
                source.configure(change)
        await asyncio.sleep(0.05)
        self.assertEqual(source.describe(), before)
        self.assertEqual(self.fake.commands, commands)
        self.assertEqual(self.changes, changes)

    async def test_a_tuner_without_gains_offers_automatic_gain_only(self):
        for fake in (FakeRtlTcp(tuner_type=5, gain_count=12), FakeRtlTcp(tuner_type=9, gain_count=29)):
            with self.subTest(tuner=fake.tuner_type, gains=fake.gain_count):
                source = await self.start(fake, gain_db=20)
                await self.read(source)
                self.assertIsNone(source.describe()["gain_db"])
                self.assertEqual(source.describe()["gains_db"], [])
                with self.assertRaises(ValueError):
                    source.configure({"gain_db": 20})
                source.configure({"gain_db": None})

    async def test_settings_out_of_the_tuner_range_are_brought_within_it(self):
        source = await self.start(FakeRtlTcp(tuner_type=4, gain_count=1))
        source.configure({"centre_hz": 2000e6})     # the tuner is not known yet
        await self.read(source)
        self.assertEqual(source.centre_hz, 924e6)
        self.assertEqual(source.describe()["tuner"], "FCI FC2580")

    async def test_changes_while_disconnected_apply_at_the_next_connection(self):
        source = await self.start()
        source.configure({"centre_hz": 433_000_000, "gain_db": 40})
        self.assertEqual(self.fake.commands, [])
        await self.read(source)
        self.assertEqual(self.fake.commands, connection_commands(0, 2_400_000, 433_000_000, 402))

    def test_refuses_invalid_settings_at_creation(self):
        for options in ({"centre_hz": 1e10}, {"gain_db": "auto"}, {"ppm": 101}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                RtlTcpSource(LOOPBACK, 1, **options)

    def test_the_header_names_the_tuner(self):
        self.assertEqual(tuner_for(6, 29).name, "Rafael Micro R828D")
        self.assertEqual(tuner_for(1, 14).range_hz, (52e6, 2200e6))
        self.assertEqual(tuner_for(0, 0).name, "Unknown tuner (type 0)")


class ShortwaveTest(SourceTest):
    async def commands_after(self, count: int) -> list[tuple[int, int]]:
        """The commands sent from the ``count``-th on, once they stop coming."""
        await asyncio.sleep(0.1)
        return self.fake.commands[count:]

    async def test_samples_directly_below_the_tuner_and_keeps_the_gain_for_it(self):
        source = await self.start()
        source.configure({"hf_input": "q", "centre_hz": 7_100_000, "gain_db": 30, "rtl_agc": True})
        self.assert_labelled(source, await self.read(source))
        self.assertEqual(self.fake.commands, connection_commands(0, 2_400_000, 7_100_000, direct=2, rtl_agc=1))
        state = source.describe()
        self.assertEqual((state["hf_input"], state["direct"], state["rtl_agc"], state["gain_db"]),
                         ("q", True, True, 29.7))
        self.assertEqual(state["range_hz"], [1.2e6, 1766e6])
        self.assertEqual((self.fake.direct_sampling, self.fake.rtl_agc), (2, 1))

    async def test_enters_and_leaves_direct_sampling_as_the_centre_frequency_crosses_the_tuner(self):
        source = await self.start(gain_db=30)
        source.configure({"hf_input": "q", "rtl_agc": True})
        await self.read(source)
        sent = len(self.fake.commands)
        self.assertEqual(self.fake.commands, connection_commands(0, 2_400_000, 145_000_000, 297))
        source.configure({"centre_hz": 7_100_000})
        self.assertEqual(await self.commands_after(sent), [(0x09, 2), (0x08, 1), (0x01, 7_100_000)])
        self.assert_labelled(source, await self.read(source))
        self.assertTrue(source.describe()["direct"])

        sent = len(self.fake.commands)
        source.configure({"gain_db": 40})                    # kept for the tuner, which sleeps
        source.configure({"hf_input": "i"})                  # the other input, on the same frequency
        source.configure({"rtl_agc": False})
        self.assertEqual(await self.commands_after(sent), [(0x09, 1), (0x01, 7_100_000), (0x08, 0)])
        self.assertEqual((source.describe()["gain_db"], source.centre_hz), (40.2, 7.1e6))

        sent = len(self.fake.commands)
        source.configure({"centre_hz": 24_000_000})          # the tuner's lowest frequency
        self.assertEqual(await self.commands_after(sent),
                         [(0x09, 0), (0x01, 24_000_000), (0x02, 2_400_000), (0x03, 1), (0x04, 402)])
        self.assert_labelled(source, await self.read(source))
        self.assertFalse(source.describe()["direct"])
        self.assertEqual((self.fake.direct_sampling, self.fake.gain_tenths), (0, 402))

        sent = len(self.fake.commands)
        source.configure({"rtl_agc": True})                  # waits for direct sampling
        source.configure({"centre_hz": 23_999_999})
        source.configure({"centre_hz": 145_000_000})
        self.assertEqual(await self.commands_after(sent), [
            (0x09, 1), (0x08, 1), (0x01, 23_999_999),
            (0x09, 0), (0x08, 0), (0x01, 145_000_000), (0x02, 2_400_000), (0x03, 1), (0x04, 402)])

        # A new sample rate while sampling directly goes once, after the tuner started again.
        sent = len(self.fake.commands)
        source.configure({"centre_hz": 7_100_000})
        source.configure({"sample_rate_hz": 1_024_000})
        source.configure({"sample_rate_hz": 2_048_000, "centre_hz": 145_000_000})
        self.assertEqual(await self.commands_after(sent), [
            (0x09, 1), (0x08, 1), (0x01, 7_100_000), (0x02, 1_024_000), (0x01, 7_100_000),
            (0x09, 0), (0x08, 0), (0x01, 145_000_000), (0x02, 2_048_000), (0x03, 1), (0x04, 402)])
        self.assertEqual((self.fake.sample_rate_hz, self.fake.gain_tenths, self.fake.tuner_state),
                         (2_048_000, 402, "rate"))

    async def test_the_reach_follows_the_input_and_the_sample_rate(self):
        source = await self.start()
        await self.read(source)
        with self.assertRaisesRegex(ValueError, "^the receiver tunes from 24 to 1766 MHz$"):
            source.configure({"centre_hz": 7_100_000})
        for hf_input in ("q", "i", "tuner"):
            with self.subTest(hf_input=hf_input):
                source.configure({"hf_input": hf_input, "centre_hz": 1_200_000})    # a span from 0 Hz
                self.assertEqual(source.describe()["range_hz"], [1.2e6, 1766e6])
                self.assertEqual(source.describe()["direct"], hf_input != "tuner")
                with self.assertRaisesRegex(ValueError, "^the receiver tunes from 1.2 to 1766 MHz$"):
                    source.configure({"centre_hz": 1_199_999})
        # A new rate or input without a centre frequency brings it within reach.
        source.configure({"sample_rate_hz": 3_200_000})
        self.assertEqual(source.describe()["centre_hz"], 1.6e6)
        source.configure({"hf_input": "none"})
        self.assertEqual(source.describe()["centre_hz"], 24e6)
        self.assertEqual(source.describe()["range_hz"], [24e6, 1766e6])
        source.configure({"hf_input": "q", "sample_rate_hz": 900_001})
        source.configure({"centre_hz": 450_001})            # whole hertz: 450,000.5 rounded up
        with self.assertRaises(ValueError):
            source.configure({"centre_hz": 450_000})

    async def test_a_tuner_far_above_shortwave_leaves_a_gap_that_is_refused(self):
        source = await self.start(FakeRtlTcp(tuner_type=1, gain_count=14))
        source.configure({"hf_input": "tuner", "centre_hz": 7_100_000})     # the tuner is not known yet
        await self.read(source)
        state = source.describe()
        # An E4000 does not cover shortwave by itself: its centre comes back within its range.
        self.assertEqual((state["hf_input"], state["hf_inputs"], state["centre_hz"], state["direct"]),
                         ("none", ["none", "q", "i"], 52e6, False))
        with self.assertRaisesRegex(ValueError, "^the shortwave input must be one of: none, q, i$"):
            source.configure({"hf_input": "tuner"})
        source.configure({"hf_input": "q"})
        self.assertEqual(source.describe()["range_hz"], [1.2e6, 2200e6])
        gap = ("^the receiver tunes from 1.2 to 27.6 MHz by direct sampling "
               "and from 52 to 2200 MHz with its tuner$")
        for centre in (27_600_001, 40e6, 51_999_999):
            with self.subTest(centre=centre), self.assertRaisesRegex(ValueError, gap):
                source.configure({"centre_hz": centre})
        source.configure({"centre_hz": 27_600_000})
        self.assertTrue(source.describe()["direct"])
        source.configure({"centre_hz": 52_000_000})
        self.assertFalse(source.describe()["direct"])

    async def test_refuses_invalid_shortwave_settings(self):
        source = await self.start()
        await self.read(source)
        before = source.describe()
        for change in ({"hf_input": "x"}, {"hf_input": None}, {"hf_input": 2}, {"hf_input": "Q"},
                       {"rtl_agc": 1}, {"rtl_agc": "true"}, {"rtl_agc": None},
                       {"hf_input": "q", "centre_hz": 1e6}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                source.configure(change)
        self.assertEqual(source.describe(), before)


class ConverterTest(SourceTest):
    """A converter in front of the receiver: every frequency of the source
    is the one on the air, and only the command is converted."""

    async def commands_after(self, count: int) -> list[tuple[int, int]]:
        """The commands sent from the ``count``-th on, once they stop coming."""
        await asyncio.sleep(0.1)
        return self.fake.commands[count:]

    async def test_sends_the_converted_frequency_for_each_converter(self):
        cases = (("up", 125_000_000, 7_100_000, 132_100_000),               # shortwave, upconverted
                 ("down", 9_750_000_000, 10_489_500_000, 739_500_000),      # a satellite LNB
                 ("down_inverted", 5_150_000_000, 3_800_000_000, 1_350_000_000))
        for converter, lo_hz, air_hz, tuned_hz in cases:
            with self.subTest(converter=converter):
                source = await self.start()
                source.configure({"converter": converter, "converter_lo_hz": lo_hz, "centre_hz": air_hz})
                samples = await self.read(source)
                self.assertEqual(self.fake.commands, connection_commands(0, 2_400_000, tuned_hz))
                self.assertEqual(source.centre_hz, air_hz)      # the samples' label: on the air
                i_code, q_code = fake_codes(tuned_hz, 2.4e6)
                if converter == "down_inverted":
                    i_code, q_code = q_code, i_code             # I and Q swapped as they arrive
                self.assertEqual(sample_codes(samples), ({i_code}, {q_code}))
                state = source.describe()
                self.assertEqual((state["converter"], state["converter_lo_hz"], state["centre_hz"]),
                                 (converter, float(lo_hz), float(air_hz)))
                source.close()
                await self.fake.__aexit__(None, None, None)

    async def test_the_reach_is_the_receiver_s_converted_and_kept_above_half_the_rate(self):
        source = await self.start()
        await self.read(source)
        source.configure({"converter": "up", "centre_hz": 20_000_000})
        # 24 - 125 MHz is below 0 Hz: the reach starts at half the sample rate.
        self.assertEqual(source.describe()["range_hz"], [1.2e6, 1641e6])
        with self.assertRaisesRegex(ValueError, "^the receiver tunes from 1.2 to 1641 MHz through its converter$"):
            source.configure({"centre_hz": 1_199_999})
        source.configure({"converter": "down", "converter_lo_hz": 9_750_000_000})
        self.assertEqual(source.describe()["range_hz"], [9774e6, 11516e6])
        source.configure({"converter": "down_inverted", "converter_lo_hz": 10_000_000_000})
        self.assertEqual(source.describe()["range_hz"], [8234e6, 9976e6])     # mirrored
        source.configure({"converter": "none"})
        self.assertEqual(source.describe()["range_hz"], [24e6, 1766e6])

    async def test_applies_on_top_of_direct_sampling(self):
        source = await self.start()
        # A downconverter whose oscillator brings 127.1 MHz to 7.1 MHz, which the Q input samples.
        source.configure({"hf_input": "q", "converter": "down", "converter_lo_hz": 120_000_000,
                          "centre_hz": 127_100_000})
        await self.read(source)
        self.assertEqual(self.fake.commands, connection_commands(0, 2_400_000, 7_100_000, direct=2))
        state = source.describe()
        self.assertEqual((state["direct"], state["range_hz"]), (True, [121.2e6, 1886e6]))
        sent = len(self.fake.commands)
        source.configure({"centre_hz": 145_000_000})        # 25 MHz: the tuner
        self.assertEqual(await self.commands_after(sent),
                         [(0x09, 0), (0x01, 25_000_000), (0x02, 2_400_000), (0x03, 0)])
        self.assertFalse(source.describe()["direct"])
        # Inverted: 22.9 MHz on the air is 7.1 MHz behind an oscillator of 30 MHz.
        sent = len(self.fake.commands)
        source.configure({"converter": "down_inverted", "converter_lo_hz": 30_000_000, "centre_hz": 22_900_000})
        self.assertEqual(await self.commands_after(sent), [(0x09, 2), (0x01, 7_100_000)])
        self.assertEqual(source.describe()["range_hz"], [1.2e6, 28.8e6])

    async def test_inverts_the_spectrum_back_for_the_reader_and_the_tap(self):
        # The receiver gets a carrier 300 kHz above its centre frequency.
        lo_hz, air_hz = 5_150_000_000, 3_800_000_000
        tuned_hz = lo_hz - air_hz
        source = await self.start(FakeRtlTcp(carriers={tuned_hz + 300_000: 0.5}, noise=0.01), idle_s=10.0)
        handed: list[tuple[bytes, float]] = []
        source.tap(lambda data, centre_hz, rate_hz, joined: handed.append((bytes(data), centre_hz)))
        # The same frequency asked of the receiver: air - lo for one, lo - air for the other.
        for converter, lo, offset_hz in (("down", air_hz - tuned_hz, 300_000), ("down_inverted", lo_hz, -300_000)):
            with self.subTest(converter=converter):
                source.configure({"converter": converter, "converter_lo_hz": lo, "centre_hz": air_hz})
                await asyncio.sleep(0.3)            # the receiver settles
                handed.clear()
                samples = await self.read(source, 8192)
                self.assertAlmostEqual(strongest_hz(samples, 2.4e6), offset_hz, delta=1000)
                await eventually(lambda: sum(len(data) for data, _ in handed) >= 2 * 8192)
                tapped = b"".join(data for data, _ in handed)[:2 * 8192]
                levels = (np.frombuffer(tapped, np.uint8).astype(np.float32) - 127.5) / 127.5
                self.assertAlmostEqual(strongest_hz(levels.view(np.complex64), 2.4e6), offset_hz, delta=1000)
                self.assertEqual({centre for _, centre in handed}, {source.describe()["centre_hz"]})

    async def test_another_converter_keeps_the_receiver_where_it_is_tuned(self):
        source = await self.start()
        await self.read(source)
        for changes, centre_hz in (({"converter": "up"}, 20e6), ({"converter_lo_hz": 120_000_000}, 25e6),
                                   ({"converter": "down", "converter_lo_hz": 9_750_000_000}, 9895e6),
                                   ({"converter": "down_inverted"}, 9605e6), ({"converter": "none"}, 145e6)):
            with self.subTest(changes=changes):
                sent = len(self.fake.commands)
                source.configure(changes)
                self.assertEqual(source.describe()["centre_hz"], centre_hz)
                self.assertEqual(await self.commands_after(sent), [(0x01, 145_000_000)])
        # The oscillator alone, without a converter, moves nothing.
        sent = len(self.fake.commands)
        source.configure({"converter_lo_hz": 100_000_000})
        self.assertEqual((source.describe()["centre_hz"], await self.commands_after(sent)), (145e6, []))

    async def test_refuses_a_conversion_beyond_the_receiver_s_reach_rather_than_clamp_it(self):
        source = await self.start()
        await self.read(source)
        before = source.describe()
        # 125 MHz minus the 145 MHz the receiver is tuned to is below 0 Hz.
        with self.assertRaisesRegex(ValueError, "^through this converter the receiver's centre frequency would be "
                                                "-20 MHz on the air, out of reach: the receiver tunes from 1.2 "
                                                "to 101 MHz through its converter$"):
            source.configure({"converter": "down_inverted"})
        self.assertEqual(source.describe(), before)
        # Behind this oscillator only 120 to 500 kHz on the air reach 24 MHz and above...
        source.configure({"sample_rate_hz": 240_000, "converter_lo_hz": 24_500_000, "converter": "down_inverted",
                          "centre_hz": 300_000})
        self.assertEqual(source.describe()["range_hz"], [120e3, 500e3])
        # ...which half of a wider rate leaves empty.
        before = source.describe()
        nothing = ("^through this converter the receiver reaches nothing on the air from 1.2 MHz, half the "
                   "sample rate, to 100 GHz$")
        for change in ({"sample_rate_hz": 2_400_000}, {"sample_rate_hz": 2_400_000, "centre_hz": 1_200_000}):
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, nothing):
                source.configure(change)
        self.assertEqual(source.describe(), before)

    async def test_refuses_invalid_converter_settings(self):
        source = await self.start()
        await self.read(source)
        before = source.describe()
        for change in ({"converter": "x"}, {"converter": None}, {"converter": 1}, {"converter": "Up"},
                       {"converter_lo_hz": 0}, {"converter_lo_hz": 0.4}, {"converter_lo_hz": -125e6},
                       {"converter_lo_hz": 100e9 + 1}, {"converter_lo_hz": float("nan")},
                       {"converter_lo_hz": float("inf")}, {"converter_lo_hz": True}, {"converter_lo_hz": "125e6"},
                       {"converter_lo_hz": None}, {"converter_lo_hz": 10**400}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                source.configure(change)
        self.assertEqual(source.describe(), before)
        source.configure({"converter_lo_hz": 100e9})        # the highest, rounded to the hertz
        source.configure({"converter_lo_hz": 0.6})
        self.assertEqual(source.describe()["converter_lo_hz"], 1.0)

    async def test_commands_stay_within_32_bits(self):
        # An unknown tuner: the command's 32 bits are its only limits.
        source = await self.start(FakeRtlTcp(tuner_type=99, gain_count=0))
        await self.read(source)
        top = 0xFFFF_FFFF
        source.configure({"converter": "up"})
        self.assertEqual(source.describe()["range_hz"], [1.2e6, top - 125e6])
        with self.assertRaisesRegex(ValueError, "^the receiver tunes from 1.2 to 4169.967295 MHz through its converter$"):
            source.configure({"centre_hz": top - 125_000_000 + 1})
        source.configure({"converter": "down", "converter_lo_hz": 9_750_000_000, "centre_hz": top + 9_750_000_000})
        await eventually(lambda: self.fake.commands[-1] == (0x01, top))

    async def test_a_converter_through_which_the_tuner_reaches_nothing_is_left(self):
        source = await self.start()
        # The tuner is not known yet: any frequency of 32 bits passes.
        source.configure({"converter": "down_inverted", "converter_lo_hz": 20_000_000, "centre_hz": 10_000_000})
        with self.assertLogs("radiolack.rtltcp", "WARNING") as logs:
            await self.read(source)
        self.assertIn("the converter is not used", logs.output[0])
        state = source.describe()
        self.assertEqual((state["converter"], state["centre_hz"], state["range_hz"]), ("none", 24e6, [24e6, 1766e6]))

    async def test_the_hardware_names_the_converter(self):
        source = await self.start()
        source.configure({"converter": "down_inverted", "converter_lo_hz": 5_150_000_000, "centre_hz": 3.8e9})
        await self.read(source)
        self.assertEqual(source.hardware, "RTL-SDR, Rafael Micro R820T, behind a downconverter that inverts the "
                                          "spectrum with its oscillator at 5150 MHz")
        source.configure({"converter": "up", "converter_lo_hz": 125_000_001, "centre_hz": 7e6})
        self.assertEqual(source.hardware, "RTL-SDR, Rafael Micro R820T, behind an upconverter with its "
                                          "oscillator at 125.000001 MHz")


class CorrectionTest(SourceTest):
    """The correction in hundredths of ppm: a receiver other than an R820T
    or R828D, here an E4000, takes its nearest whole number by command
    0x05, and the source asks for a frequency scaled for the rest,
    labelling the samples with the one on the air. An R820T takes none
    (GridTest)."""

    async def start(self, fake: FakeRtlTcp | None = None, **options) -> RtlTcpSource:
        return await super().start(fake or FakeRtlTcp(tuner_type=1, gain_count=14), **options)

    async def commands_after(self, count: int) -> list[tuple[int, int]]:
        """The commands sent from the ``count``-th on, once they stop coming."""
        await asyncio.sleep(0.1)
        return self.fake.commands[count:]

    async def test_a_whole_correction_sends_what_it_always_did(self):
        source = await self.start(ppm=7, gain_db=40)
        await self.read(source)
        self.assertEqual(self.fake.commands, connection_commands(7, 2_400_000, 145_000_000, 420))
        for ppm in (12, -100, 100, 0):
            with self.subTest(ppm=ppm):
                sent = len(self.fake.commands)
                source.configure({"ppm": ppm})
                self.assertEqual(await self.commands_after(sent), [(0x05, ppm % 2**32), (0x01, 145_000_000)])

    async def test_hundredths_alone_retune_without_the_correction_command(self):
        source = await self.start(ppm=5)
        await self.read(source)
        # Each step, with what the receiver takes and the frequency asked.
        for ppm, whole, requested in ((5.25, None, 144_999_964), (5.49, None, 144_999_929),
                                      (5.5, 6, 145_000_072), (6.0, None, 145_000_000),
                                      (-0.5, -1, 144_999_927), (0.5, 1, 145_000_072), (0.49, 0, 144_999_929)):
            with self.subTest(ppm=ppm):
                sent = len(self.fake.commands)
                changed = time.monotonic()
                source.configure({"ppm": ppm})
                expected = ([] if whole is None else [(0x05, whole % 2**32)]) + [(0x01, requested)]
                self.assertEqual(await self.commands_after(sent), expected)
                self.assertEqual(source.describe()["ppm"], ppm)
                await self.read(source)         # the samples of the previous setting are dropped
                self.assertGreaterEqual(time.monotonic() - changed, FAST["settle_s"])
                self.assertEqual(source.centre_hz, 145e6)       # the label: the frequency asked

    async def test_each_connection_sends_the_whole_part_then_the_scaled_frequency(self):
        source = await self.start(ppm=99.5)
        await self.read(source)
        self.assertEqual(self.fake.commands, connection_commands(100, 2_400_000, 145_000_072))
        self.assertEqual(source.describe()["ppm"], 99.5)

    async def test_the_request_stays_within_32_bits_and_the_tuner_s_range(self):
        # An unknown tuner: the command's 32 bits are the limit.
        source = await self.start(FakeRtlTcp(tuner_type=9), centre_hz=0xFFFF_FFFF, ppm=-0.4)
        await self.read(source)
        self.assertEqual(self.fake.commands[-4], (0x01, 0xFFFF_FFFF))
        await self.fake.__aexit__(None, None, None)
        # An R820T: kept within 24 to 1766 MHz on its tuner, so that the
        # RTL-SDR Blog driver does not switch to direct sampling below it.
        source = await self.start(FakeRtlTcp(), centre_hz=24_000_000, ppm=0.4)
        await self.read(source)
        self.assertEqual(self.fake.commands[-4], (0x01, 24_000_000))
        # A centre whose request stays the same retunes nothing (None).
        cases = (({"centre_hz": 24_000_001}, None), ({"centre_hz": 30_000_000}, 29_999_988),
                 ({"centre_hz": 1_766_000_000, "ppm": -0.4}, 1_766_000_000),
                 ({"centre_hz": 1_765_000_000}, 1_765_000_706),
                 # A tuner that covers shortwave goes below 24 MHz by itself.
                 ({"hf_input": "tuner", "centre_hz": 24_000_000, "ppm": 0.4}, 23_999_990),
                 # Direct sampling keeps the scaled frequency, even at 24 MHz.
                 ({"hf_input": "q", "centre_hz": 7_100_000}, 7_099_997),
                 ({"centre_hz": 23_999_999, "ppm": -0.4}, 24_000_009))
        for changes, requested in cases:
            with self.subTest(changes=changes):
                sent = len(self.fake.commands)
                source.configure(changes)
                commands = await self.commands_after(sent)
                if requested is None:
                    self.assertEqual(commands, [])
                else:
                    self.assertEqual(commands[-1], (0x01, requested))
        self.assertTrue(source.describe()["direct"])
        self.assertEqual(self.fake.direct_sampling, 2)

    async def test_a_carrier_shows_at_its_frequency_with_the_crystal_s_error_corrected(self):
        rate_hz, offset_hz = 2.4e6, 10_000.0
        # The frequency on the air, the converter, and where the receiver tunes.
        paths = (("tuner", 145_000_000, {}, 145_000_000),
                 ("direct sampling", 7_100_000, {"hf_input": "q"}, 7_100_000),
                 ("upconverter", 7_100_000, {"converter": "up", "converter_lo_hz": 125_000_000}, 132_100_000),
                 ("inverted LNB", 10_489_500_000,
                  {"converter": "down_inverted", "converter_lo_hz": 11_229_000_000}, 739_500_000))
        for crystal_ppb in (37_420, -52_730):
            for path, air_hz, settings, tuned_hz in paths:
                with self.subTest(crystal_ppb=crystal_ppb, path=path):
                    # The carrier where the receiver gets it: mirrored behind the inverted LNB.
                    carrier = tuned_hz - offset_hz if settings.get("converter") == "down_inverted" else tuned_hz + offset_hz
                    fake = FakeRtlTcp(carriers={carrier: 0.5}, noise=0.01, chunk_bytes=8000,
                                      crystal_ppb=crystal_ppb)
                    source = await self.start(fake, idle_s=10.0)
                    source.configure({**settings, "centre_hz": air_hz, "ppm": crystal_ppb / 1000})
                    samples = await self.read(source, 48_000)
                    self.assertEqual(source.centre_hz, air_hz)
                    self.assertAlmostEqual(carrier_hz(samples, rate_hz, offset_hz), offset_hz, delta=1.0)
                    if path == "tuner":
                        # The whole part alone leaves it tens of hertz off: the fake's crystal moves it.
                        source.configure({"ppm": round(crystal_ppb / 1000)})
                        await asyncio.sleep(0.3)
                        error_hz = carrier_hz(await self.read(source, 48_000), rate_hz, offset_hz) - offset_hz
                        self.assertGreater(abs(error_hz), 30.0)
                    source.close()
                    await self.fake.__aexit__(None, None, None)


class TunerRestartTest(SourceTest):
    """librtlsdr starts the tuner again on command 0x09 with 0, which the
    source sends at each connection and on leaving direct sampling: the
    tuner then runs at r82xx_init's IF until the next sample rate. With a
    tuner that follows the driver's PLL computation, a carrier sits where
    the IF of the sample rate puts it, as it would not if the sample rate
    came before 0x09."""

    CENTRE_HZ = 145_000_000
    OFFSET_HZ = 10_000.0

    def expected_hz(self, rate_hz: int, state: str) -> float:
        """Where the carrier shows from the centre frequency, the tuner in ``state``."""
        return self.OFFSET_HZ - (r82xx.tuned_hz("truncating", self.CENTRE_HZ, rate_hz, 0, state) - self.CENTRE_HZ)

    async def measure(self, source: RtlTcpSource) -> float:
        samples = await self.read(source, 48_000)
        return carrier_hz(samples, source.sample_rate_hz, self.OFFSET_HZ)

    async def test_the_carrier_follows_the_if_of_the_sample_rate(self):
        # At this centre the IF of each rate and r82xx_init's put the carrier tens of hertz apart.
        for rate in (2_400_000, 1_024_000):
            self.assertGreater(abs(self.expected_hz(rate, "rate") - self.expected_hz(rate, "started")), 40)
        fake = FakeRtlTcp(carriers={self.CENTRE_HZ + self.OFFSET_HZ: 0.5}, noise=0.01, chunk_bytes=8000,
                          tuner_pll=functools.partial(r82xx.tuned_hz, "truncating"))
        source = await self.start(fake, centre_hz=self.CENTRE_HZ, gain_db=30, idle_s=10.0)
        source.configure({"hf_input": "q"})

        # At connection, after 0x09 with 0.
        self.assertAlmostEqual(await self.measure(source), self.expected_hz(2_400_000, "rate"), delta=2.0)

        # After a new sample rate.
        source.configure({"sample_rate_hz": 1_024_000})
        self.assertAlmostEqual(await self.measure(source), self.expected_hz(1_024_000, "rate"), delta=2.0)

        # After direct sampling, entered and left.
        source.configure({"centre_hz": 7_100_000})
        await self.read(source)
        await eventually(lambda: self.fake.direct_sampling == 2)
        source.configure({"centre_hz": self.CENTRE_HZ})
        self.assertAlmostEqual(await self.measure(source), self.expected_hz(1_024_000, "rate"), delta=2.0)
        self.assertEqual((self.fake.direct_sampling, self.fake.tuner_state, self.fake.gain_tenths), (0, "rate", 297))


def on_grid(frequency_hz: float, rate_hz: int, low_hz: float = 24e6) -> int:
    """Where an R820T's request goes on its grid at ``rate_hz``."""
    return r82xx.on_grid(frequency_hz, r82xx.rate_if_hz(rate_hz), low_hz, 1766e6)


class GridTest(SourceTest):
    """An R820T or R828D on its tuner, placed on its grid: the oscillator a
    multiple of 112.5 kHz, the samples labelled where the tuner sits, the
    whole of the correction Radiolack's."""

    async def start(self, fake: FakeRtlTcp | None = None, **options) -> RtlTcpSource:
        return await super().start(fake, grid=True, **options)

    async def commands_after(self, count: int) -> list[tuple[int, int]]:
        """The commands sent from the ``count``-th on, once they stop coming."""
        await asyncio.sleep(0.1)
        return self.fake.commands[count:]

    async def test_places_the_centre_where_the_oscillator_is_on_the_grid_at_every_rate(self):
        source = await self.start(gain_db=30)
        self.assert_labelled(source, await self.read(source))
        # 2.5 kHz below 145 MHz at 2.4 MS/s, whose IF is 1.815 MHz.
        self.assertEqual(self.fake.commands, connection_commands(0, 2_400_000, 144_997_500, 297))
        state = source.describe()
        self.assertEqual((state["centre_hz"], state["centre_in_use_hz"], state["tuner_grid"]),
                         (145e6, 144_997_500.0, True))
        self.assertEqual((source.centre_hz, source.exact_centre_hz, source.grid_hz), (145e6, 144_997_500.0, 112_500))
        for rate in (1_024_000, 240_000, 300_000, 2_400_000):
            with self.subTest(rate=rate):
                source.configure({"sample_rate_hz": rate})       # the IF follows the rate: so does the grid
                self.assert_labelled(source, await self.read(source))
                request = self.fake.frequency_hz
                self.assertEqual((request + r82xx.rate_if_hz(rate)) % 112_500, 0)
                self.assertLessEqual(abs(request - 145e6), 56_250)
                self.assertEqual((source.exact_centre_hz, source.describe()["centre_in_use_hz"]),
                                 (request, request))
        self.assertEqual(source.centre_in_use(433e6), on_grid(433e6, 2_400_000))
        # Off the grid, every centre is asked as it is.
        sent = len(self.fake.commands)
        source.configure({"tuner_grid": False})
        self.assertEqual(await self.commands_after(sent), [(0x01, 145_000_000)])
        self.assertEqual((source.describe()["centre_in_use_hz"], source.grid_hz), (145e6, 0.0))

    async def test_labels_by_the_driver_s_computation_without_retuning(self):
        source = await self.start(centre_hz=433_000_000)
        await self.read(source)
        request = on_grid(433e6, 2_400_000)
        sent = len(self.fake.commands)
        for driver in ("truncating", "rounding", "unknown"):
            with self.subTest(driver=driver):
                source.configure({"driver_pll": driver})
                self.assert_labelled(source, await self.read(source))
                tuned = request if driver == "unknown" else r82xx.tuned_hz(driver, request, 2_400_000)
                self.assertEqual(source.exact_centre_hz, tuned)
                self.assertEqual(source.describe()["driver_pll"], driver)
        # One constant low by the truncating computation, none by the rounding one, on the grid.
        self.assertAlmostEqual(r82xx.tuned_hz("truncating", request, 2_400_000) - request, -219.7, delta=0.1)
        self.assertEqual(r82xx.tuned_hz("rounding", request, 2_400_000), request)
        self.assertEqual(await self.commands_after(sent), [])      # nothing asked of the receiver
        # Off the grid, the computation labels each centre with its own error.
        source.configure({"driver_pll": "truncating", "tuner_grid": False})
        await self.read(source)
        self.assertEqual(source.exact_centre_hz, r82xx.tuned_hz("truncating", 433_000_000, 2_400_000))

    async def test_takes_the_whole_correction_itself_and_scales_the_rate(self):
        ppm = 12.5
        scale = 1 + ppm / 1e6
        air_hz = 145_020_000.0
        fake = FakeRtlTcp(carriers={air_hz: 0.5}, noise=0.01, chunk_bytes=8000, crystal_ppb=12_500,
                          tuner_pll=functools.partial(r82xx.tuned_hz, "rounding"))
        source = await self.start(fake, ppm=ppm, gain_db=30, idle_s=10.0)
        source.configure({"driver_pll": "rounding"})
        samples = await self.read(source, 48_000)
        request = on_grid(round(145e6 / scale), 2_400_000)
        self.assertEqual(self.fake.commands, connection_commands(0, 2_400_000, request, 297))     # 0x05 with 0
        self.assertEqual(source.rate_scale, scale)
        self.assertAlmostEqual(source.exact_centre_hz, request * scale, places=4)
        # The carrier, measured in samples at the rate asked, lies a rate_scale nearer.
        offset = carrier_hz(samples, source.sample_rate_hz, (air_hz - source.exact_centre_hz) / scale)
        self.assertAlmostEqual(source.exact_centre_hz + offset * scale, air_hz, delta=0.2)
        # A correction that keeps the request on the same point of the grid retunes nothing.
        sent = len(self.fake.commands)
        source.configure({"ppm": 12.6})
        self.assertEqual(await self.commands_after(sent), [])
        await self.read(source)
        self.assertAlmostEqual(source.exact_centre_hz, request * (1 + 12.6e-6), places=4)

    async def test_leaves_direct_sampling_other_tuners_and_an_upconverting_v4_unplaced(self):
        cases = (
            # A tuner other than an R820T or R828D: the correction's whole part is its own.
            ("E4000", FakeRtlTcp(tuner_type=1, gain_count=14), {"ppm": 3}, 145_000_000, 3),
            ("direct sampling", FakeRtlTcp(), {"hf_input": "q", "centre_hz": 7_100_000}, 7_100_000, 0),
            # Below 28.8 MHz an RTL-SDR Blog V4 adds 28.8 MHz in its driver: not computed.
            ("R828D below 28.8 MHz", FakeRtlTcp(tuner_type=6), {"hf_input": "tuner", "centre_hz": 14_200_000},
             14_200_000, 0),
            # Below 27.66 MHz the oscillator lies under the VCO's range over 64: not computed either.
            ("R820T at 24 MHz", FakeRtlTcp(), {"centre_hz": 24_000_000}, 24_000_000, 0),
        )
        for name, fake, settings, request, whole in cases:
            with self.subTest(name=name):
                source = await self.start(fake, gain_db=None)
                source.configure({**settings, "driver_pll": "truncating"})
                self.assert_labelled(source, await self.read(source))
                self.assertEqual(self.fake.frequency_hz, request)
                self.assertEqual(self.fake.correction_ppm, whole)
                self.assertEqual(source.describe()["centre_in_use_hz"], request)
                self.assertEqual(source.grid_hz, 0.0 if name == "E4000" else 112_500)
                source.close()
        # The same R828D at 145 MHz is placed.
        source = await self.start(FakeRtlTcp(tuner_type=6))
        await self.read(source)
        self.assertEqual(self.fake.frequency_hz, 144_997_500)

    async def test_a_converter_places_the_receiver_s_own_frequency(self):
        cases = (("up", 125_000_000, 20_000_000, lambda tuned: tuned - 125e6),
                 ("down_inverted", 5_150_000_000, 3_800_000_000, lambda tuned: 5150e6 - tuned))
        for converter, lo_hz, air_hz, air in cases:
            with self.subTest(converter=converter):
                source = await self.start()
                source.configure({"converter": converter, "converter_lo_hz": lo_hz, "centre_hz": air_hz})
                await self.read(source)
                request = self.fake.frequency_hz
                self.assertEqual((request + r82xx.rate_if_hz(2_400_000)) % 112_500, 0)
                self.assertEqual(source.exact_centre_hz, air(request))
                self.assertEqual(source.centre_hz, air_hz)
                self.assertEqual(source.centre_in_use(air_hz), air(request))
                source.close()

    async def test_places_within_the_range_at_its_top(self):
        source = await self.start(centre_hz=1_766_000_000)
        await self.read(source)
        # The highest oscillator on the grid within the range: 102.5 kHz below its top.
        self.assertEqual(self.fake.frequency_hz, 1_765_897_500)
        self.assertEqual(source.describe()["centre_in_use_hz"], 1_765_897_500)

    async def test_refuses_invalid_driver_and_grid_settings(self):
        source = await self.start()
        await self.read(source)
        before = source.describe()
        for change in ({"driver_pll": "newer"}, {"driver_pll": None}, {"driver_pll": 1},
                       {"tuner_grid": "yes"}, {"tuner_grid": 1}, {"tuner_grid": None}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                source.configure(change)
        self.assertEqual(source.describe(), before)


class AntennaPowerTest(SourceTest):
    async def test_starts_off_is_sent_at_each_connection_and_when_it_changes(self):
        source = await self.start()
        await self.read(source)
        self.assertEqual(self.fake.commands[-1], (0x0e, 0))
        self.assertFalse(source.describe()["bias_tee"])
        source.configure({"bias_tee": True})
        await eventually(lambda: self.fake.commands[-1] == (0x0e, 1))
        self.assertTrue(source.describe()["bias_tee"])
        self.fake.drop()        # a reconnection applies the state in use
        # The fake receives the commands a moment after the source sends them.
        await eventually(lambda: self.fake.connections == 2 and len(self.fake.commands) == 2 * CONNECTION + 1)
        self.assertEqual(self.fake.commands[-CONNECTION:],
                         connection_commands(0, 2_400_000, 145_000_000, bias_tee=1))
        source.configure({"bias_tee": False})
        await eventually(lambda: self.fake.commands[-1] == (0x0e, 0))

    async def test_turns_it_off_before_closing(self):
        source = await self.start()
        source.configure({"bias_tee": True})
        await self.read(source)
        source.close()
        await eventually(lambda: self.fake.open_connections == 0)
        self.assertEqual(self.fake.commands[CONNECTION:], [(0x0e, 0)])

    async def test_a_source_without_power_closes_without_a_command(self):
        source = await self.start()
        await self.read(source)
        source.close()
        await eventually(lambda: self.fake.open_connections == 0)
        self.assertEqual(len(self.fake.commands), CONNECTION)

    async def test_turns_it_off_on_the_server_it_leaves_for_another_address(self):
        source = await self.start()
        source.configure({"bias_tee": True})
        await self.read(source)
        other = FakeRtlTcp()
        await other.__aenter__()
        self.addAsyncCleanup(other.__aexit__, None, None, None)
        source.configure({"address": f"{LOOPBACK}:{other.port}"})
        await self.read(source)
        await eventually(lambda: self.fake.open_connections == 0)
        self.assertEqual(self.fake.commands[CONNECTION:], [(0x0e, 0)])
        self.assertEqual(other.commands[-1], (0x0e, 1))     # the state in use

    async def test_refuses_anything_but_true_or_false(self):
        source = await self.start()
        for value in (1, 0, "on", None):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "antenna power"):
                source.configure({"bias_tee": value})
        self.assertFalse(source.describe()["bias_tee"])


class AddressTest(SourceTest):
    async def other_server(self) -> FakeRtlTcp:
        """A second fake server, closed after the test."""
        fake = FakeRtlTcp()
        await fake.__aenter__()
        self.addAsyncCleanup(fake.__aexit__, None, None, None)
        return fake

    async def test_a_new_address_hangs_up_and_connects_there_with_the_same_settings(self):
        source = await self.start(centre_hz=433_000_000, gain_db=40, ppm=-4)
        await self.read(source)
        other = await self.other_server()
        source.configure({"address": f"{LOOPBACK}:{other.port}"})
        state = source.describe()
        self.assertEqual((state["address"], state["status"], state["tuner"]),
                         (f"{LOOPBACK}:{other.port}", "connecting", None))
        self.assert_labelled(source, await self.read(source))
        self.assertEqual(source.status, "streaming")
        # An R820T: the whole correction by the frequency asked.
        self.assertEqual(other.commands, connection_commands(0, 2_400_000, 433_001_732, 402))
        await eventually(lambda: self.fake.open_connections == 0)
        self.assertEqual(self.fake.connections, 1)

    async def test_a_new_address_while_waiting_is_tried_at_once(self):
        source = RtlTcpSource(LOOPBACK, free_port(), **{**FAST, "retry_s": (5.0, 5.0)})
        self.addCleanup(source.close)
        self.assertIsNone(await source.read(COUNT))
        await eventually(lambda: source.status == "waiting")
        other = await self.other_server()
        changed = time.monotonic()
        source.configure({"address": str(other.port)})     # on 127.0.0.1, rtl_tcp's default host
        self.assert_labelled(source, await self.read(source))
        self.assertLess(time.monotonic() - changed, 2.0)    # not after the 5 s wait before a retry

    async def test_the_address_in_use_changes_nothing(self):
        source = await self.start()
        await self.read(source)
        for same in (f"{LOOPBACK}:{self.fake.port}", str(self.fake.port), f" {LOOPBACK}:{self.fake.port} "):
            source.configure({"address": same})
        self.assert_labelled(source, await self.read(source))
        self.assertEqual((source.status, self.fake.connections), ("streaming", 1))

    def test_without_an_address_the_default_one_waits_for_the_saved_one(self):
        source = RtlTcpSource()     # nothing reads: nothing connects
        self.assertEqual((source.address, source.given), ("127.0.0.1:1234", frozenset()))
        source.configure({"address": "192.0.2.10"})     # as the engine restores a saved address
        self.assertEqual((source.address, source.status), ("192.0.2.10:1234", "idle"))


class TapTest(SourceTest):
    """The raw samples that an I/Q recording takes: whole I/Q pairs as
    rtl_tcp sends them, labelled with their setting, whether the source's
    reader takes them or not, with a gap after a retune or a new
    connection."""

    def tap(self, source: RtlTcpSource) -> list[tuple[bytes, float, float, bool]]:
        """What the source hands over from now on, in order."""
        handed: list[tuple[bytes, float, float, bool]] = []
        source.tap(lambda data, centre_hz, rate_hz, joined: handed.append((bytes(data), centre_hz, rate_hz, joined)))
        return handed

    def assert_pairs(self, handed: list[tuple[bytes, float, float, bool]]) -> None:
        """Each hand-over holds whole I/Q pairs, taken with the setting it gives."""
        for data, centre_hz, rate_hz, _ in handed:
            self.assertEqual(len(data) % 2, 0)
            i_code, q_code = fake_codes(centre_hz, rate_hz)
            self.assertEqual((set(data[0::2]), set(data[1::2])), ({i_code}, {q_code}))

    async def test_hands_whole_pairs_labelled_with_their_setting(self):
        source = await self.start(centre_hz=433_000_000, sample_rate_hz=1_024_000, idle_s=10.0)
        self.assertEqual(source.hardware, "RTL-SDR")        # until rtl_tcp names its tuner
        handed = self.tap(source)
        await self.read(source)
        # Every later chunk of the fake rtl_tcp starts with a Q byte.
        await eventually(lambda: sum(len(data) for data, *_ in handed) >= 20_000)
        self.assert_pairs(handed)
        self.assertEqual({(centre, rate) for _, centre, rate, _ in handed}, {(433e6, 1_024_000.0)})
        self.assertEqual([joined for *_, joined in handed], [False] + [True] * (len(handed) - 1))
        self.assertEqual(source.hardware, "RTL-SDR, Rafael Micro R820T")

    async def test_hands_the_samples_that_the_reader_leaves(self):
        source = await self.start(buffer_s=0.01, idle_s=10.0)
        handed = self.tap(source)
        await self.read(source)         # then no more reads
        kept = 2 * round(0.01 * 2.4e6)  # what the source keeps for its reader, in bytes
        await eventually(lambda: sum(len(data) for data, *_ in handed) > 4 * kept)
        self.assertTrue(all(joined for *_, joined in handed[1:]))

    async def test_a_retune_leaves_a_gap_then_hands_the_new_setting(self):
        source = await self.start(idle_s=10.0)
        handed = self.tap(source)
        await self.read(source)
        await eventually(lambda: handed)
        source.configure({"centre_hz": 433_000_000})
        await eventually(lambda: sum(len(data) for data, centre, *_ in handed if centre == 433e6) >= 10_000)
        self.assert_pairs(handed)
        first = next(index for index, (_, centre, _, _) in enumerate(handed) if centre == 433e6)
        self.assertEqual({centre for _, centre, _, _ in handed[:first]}, {145e6})
        self.assertEqual([joined for *_, joined in handed[first:]], [False] + [True] * (len(handed) - first - 1))

    async def test_a_new_connection_leaves_a_gap(self):
        source = await self.start(idle_s=10.0)
        handed = self.tap(source)
        await self.read(source)
        await eventually(lambda: handed)
        self.fake.drop()
        await eventually(lambda: self.fake.connections == 2 and [h for h in handed if not h[3]][1:])
        self.assert_pairs(handed)

    async def test_stops_handing_once_untapped(self):
        source = await self.start(idle_s=10.0)
        handed = self.tap(source)
        await self.read(source)
        await eventually(lambda: handed)
        source.tap(None)
        count = len(handed)
        await asyncio.sleep(0.1)
        self.assertEqual(len(handed), count)

    async def test_a_tap_that_fails_is_left_and_the_samples_go_on(self):
        source = await self.start()
        calls = []

        def fail(*handed):
            calls.append(handed)
            raise RuntimeError("a broken recording")

        source.tap(fail)
        with self.assertLogs("radiolack.rtltcp", "ERROR"):
            self.assert_labelled(source, await self.read(source))
        self.assert_labelled(source, await self.read(source))
        self.assertEqual(len(calls), 1)


class AddressParsingTest(unittest.TestCase):
    def test_reads_a_host_a_port_or_both(self):
        addresses = {
            "40000": (LOOPBACK, 40000),
            "radio.example": ("radio.example", 1234),
            " 192.0.2.10:40000 ": ("192.0.2.10", 40000),
            "[::1]": ("::1", 1234),
            "[fe80::1%eth0]:40000": ("fe80::1%eth0", 40000),
        }
        for text, address in addresses.items():
            with self.subTest(text=text):
                self.assertEqual(parse_address(text), address)

    def test_says_why_it_refuses_an_address(self):
        reasons = {
            " ": "no address given",
            "192.0.2.10 40000": "the address must be HOST, PORT or HOST:PORT",
            ":40000": "the address must be HOST, PORT or HOST:PORT",
            "::1": "an IPv6 address goes in brackets",
            "[::1]40000": "an IPv6 address goes in brackets",
            "host:0": "the port must be a whole number from 1 to 65535",
            "host:" + "9" * 5000: "the port must be a whole number from 1 to 65535",
            "x" * 254: "the host name is too long",
        }
        for text, reason in reasons.items():
            with self.subTest(text=text[:20]), self.assertRaisesRegex(ValueError, reason):
                parse_address(text)


class ConnectionTest(SourceTest):
    async def test_waits_while_refused_then_connects(self):
        port = free_port()
        source = RtlTcpSource(LOOPBACK, port, **FAST)
        self.addCleanup(source.close)
        self.assertIsNone(await source.read(COUNT))
        await eventually(lambda: source.status == "waiting")
        self.assertEqual(source.detail, "connection refused")
        async with FakeRtlTcp(port=port) as fake:
            self.fake = fake
            samples = await self.read(source)
            self.assert_labelled(source, samples)
            self.assertEqual(source.status, "streaming")
            source.close()

    async def test_reconnects_after_a_lost_connection_and_sends_the_settings_again(self):
        source = await self.start()
        source.configure({"centre_hz": 433_000_000})
        await self.read(source)
        self.fake.drop()
        await eventually(lambda: self.fake.connections == 2 and source.status == "streaming")
        self.assert_labelled(source, await self.read(source))
        self.assertEqual(self.fake.commands[CONNECTION:], self.fake.commands[:CONNECTION])

    async def test_a_lost_connection_is_a_waiting_state(self):
        source = await self.start()
        await self.read(source)
        statuses = []
        source.on_change = lambda: statuses.append((source.status, source.detail))
        self.fake.drop()
        await eventually(lambda: ("streaming", "") in statuses)
        self.assertEqual(statuses[0], ("waiting", "connection lost"))

    async def test_another_server_is_not_rtl_tcp(self):
        source = await self.start(FakeRtlTcp(magic=b"SSH-"))
        self.assertIsNone(await source.read(COUNT))
        await eventually(lambda: source.status == "waiting")
        self.assertEqual(source.detail, "not an rtl_tcp server")

    async def test_a_server_that_never_answers(self):
        source = await self.start(FakeRtlTcp(header=False))
        while source.status != "waiting":
            self.assertIsNone(await source.read(COUNT))
        self.assertEqual(source.detail, "no answer (rtl_tcp may be serving another client)")

    async def test_a_silent_server_is_left_then_tried_again(self):
        source = await self.start()
        await self.read(source)
        statuses = []
        source.on_change = lambda: statuses.append((source.status, source.detail))
        self.fake.sending = False
        # Samples on their way when the server fell silent may still come,
        # the more so on a busy computer; once a read finds none, none come
        # until the source leaves the server.
        silent, deadline = False, time.monotonic() + 5.0
        while ("waiting", "no samples") not in statuses:
            self.assertLess(time.monotonic(), deadline, f"still {source.status} ({source.detail})")
            samples = await source.read(COUNT)
            if silent:
                self.assertIsNone(samples)
            silent = samples is None
        self.fake.sending = True
        self.assert_labelled(source, await self.read(source))
        self.assertGreaterEqual(self.fake.connections, 2)

    async def test_hangs_up_when_nobody_reads_and_connects_again_at_the_next_read(self):
        source = await self.start(idle_s=0.3)
        await self.read(source)
        await eventually(lambda: self.fake.open_connections == 0)
        await eventually(lambda: source.status == "idle")
        self.assert_labelled(source, await self.read(source))
        self.assertEqual(self.fake.connections, 2)

    async def test_close_hangs_up_for_good(self):
        source = await self.start()
        await self.read(source)
        source.close()
        await eventually(lambda: self.fake.open_connections == 0)
        self.assertIsNone(await source.read(COUNT))
        await asyncio.sleep(0.1)
        self.assertEqual(self.fake.connections, 1)


@unittest.skipUnless(os.name == "posix", "POSIX processes and signals")
class RtlSdrSourceTest(SourceTest):
    """RtlSdrSource running the fake rtl_tcp program in place of rtl_tcp."""

    def setUp(self):
        self.runs = FakeRtlTcpRuns(self)

    async def start_sdr(self, device: str = "0", **options) -> RtlSdrSource:
        # A whole Python starts the fake program: give it time.
        source = RtlSdrSource(device, program=FAKE_RTL_TCP,
                              **{**FAST, "timeout_s": 5.0, "stop_s": 2.0, **options})

        async def close():
            source.close()
            await asyncio.wait_for(source.wait_closed(), 10)

        self.addAsyncCleanup(close)
        return source

    async def read_until(self, source: RtlSdrSource, condition, timeout: float = 10.0) -> None:
        """Read until ``condition()`` holds: reading keeps the source trying."""
        deadline = time.monotonic() + timeout
        while not condition():
            if time.monotonic() > deadline:
                raise AssertionError(f"the condition never held: {source.status} ({source.detail})")
            await source.read(COUNT)

    def assert_ended(self, pid: int) -> None:
        """The process has ended, and its parent has collected its exit status."""
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    async def test_stops_rtl_tcp_once_it_has_turned_the_antenna_power_off(self):
        source = await self.start_sdr()
        source.configure({"bias_tee": True})
        await self.read(source, timeout=10)
        [(pid, _)] = self.runs.started()
        source.close()
        await asyncio.wait_for(source.wait_closed(), 10)
        events = self.runs.events(pid)
        self.assertEqual(events[-2:], ["command 14 0", "stopping"])
        self.assertIn("command 14 1", events)

    async def test_runs_rtl_tcp_on_a_free_loopback_port_and_streams(self):
        source = await self.start_sdr()
        self.assertEqual(self.runs.started(), [])
        self.assert_labelled(source, await self.read(source, timeout=10))
        [(_, arguments)] = self.runs.started()
        self.assertRegex(arguments, r"^-a 127\.0\.0\.1 -p [1-9]\d* -n 10 -d 0$")
        state = source.describe()
        self.assertEqual((state["kind"], state["status"], state["address"], state["tuner"]),
                         ("rtl_sdr", "streaming", None, "Rafael Micro R820T"))

    async def test_stops_rtl_tcp_when_nobody_reads_and_starts_it_again_at_the_next_read(self):
        source = await self.start_sdr(idle_s=0.3)
        await self.read(source, timeout=10)
        [(first, _)] = self.runs.started()
        await eventually(lambda: source.status == "idle")
        self.assertEqual(self.runs.stopping(), {first})
        self.assert_ended(first)
        self.assert_labelled(source, await self.read(source, timeout=10))
        self.assertEqual(len(self.runs.started()), 2)

    async def test_close_stops_rtl_tcp(self):
        source = await self.start_sdr()
        await self.read(source, timeout=10)
        [(pid, _)] = self.runs.started()
        source.close()
        await asyncio.wait_for(source.wait_closed(), 10)
        self.assertEqual(self.runs.stopping(), {pid})
        self.assert_ended(pid)
        self.assertIsNone(await source.read(COUNT))

    async def test_a_receiver_rtl_tcp_cannot_open_is_a_waiting_state_with_the_reason(self):
        reasons = {
            "none": "no receiver found",
            "busy": "the receiver is in use by another program",
            "denied": "no permission to open the receiver",
        }
        for device, reason in reasons.items():
            with self.subTest(device=device):
                source = await self.start_sdr(device)
                await self.read_until(source, lambda: source.status == "waiting")
                self.assertEqual(source.detail, reason)
                runs = len(self.runs.started())
                await self.read_until(source, lambda: len(self.runs.started()) > runs)
                source.close()
                await asyncio.wait_for(source.wait_closed(), 10)
        for pid, _ in self.runs.started():
            self.assert_ended(pid)

    async def test_an_unknown_failure_gives_the_exit_code_and_logs_the_messages(self):
        source = await self.start_sdr("crash")
        with self.assertLogs("radiolack.rtltcp", "WARNING") as logs:
            await self.read_until(source, lambda: source.status == "waiting")
        self.assertEqual(source.detail, "rtl_tcp stopped (exit code 3)")
        self.assertIn("Something unexpected happened", logs.output[0])

    async def test_rtl_tcp_that_never_listens_is_stopped_then_tried_again(self):
        source = await self.start_sdr("mute", timeout_s=1.5)
        await self.read_until(source, lambda: source.status == "waiting")
        self.assertEqual(source.detail, "rtl_tcp did not start")
        first = self.runs.started()[0][0]
        self.assertIn(first, self.runs.stopping())
        self.assert_ended(first)
        await self.read_until(source, lambda: len(self.runs.started()) > 1)

    async def test_rtl_tcp_that_ignores_sigterm_is_killed(self):
        source = await self.start_sdr("stubborn", stop_s=0.5)
        await self.read(source, timeout=10)
        [(pid, _)] = self.runs.started()
        with self.assertLogs("radiolack.rtltcp", "INFO") as logs:     # expected: no warning
            source.close()
            await asyncio.wait_for(source.wait_closed(), 10)
        self.assertEqual(logs.output, ["INFO:radiolack.rtltcp:rtl_tcp did not stop within 0.5 s: killing it"])
        self.assert_ended(pid)
        self.assertEqual(self.runs.stopping(), set())

    async def test_closing_while_an_idle_source_stops_rtl_tcp_still_kills_it(self):
        # Radiolack ending a moment after its last interface closed: the
        # source, idle, has asked rtl_tcp to stop and waits before killing it.
        source = await self.start_sdr("stubborn", stop_s=1.0)
        await self.read(source, timeout=10)
        [(pid, _)] = self.runs.started()
        await eventually(lambda: "ignoring" in self.runs.events(pid), timeout=10)
        source.close()
        await asyncio.wait_for(source.wait_closed(), 10)
        self.assert_ended(pid)

    async def test_a_missing_program_is_a_waiting_state(self):
        source = RtlSdrSource(program=("/nonexistent/rtl_tcp",), **FAST)
        self.addCleanup(source.close)
        await self.read_until(source, lambda: source.status == "waiting")
        self.assertEqual(source.detail, "rtl_tcp is not installed")

    def test_needs_a_receiver_and_a_program(self):
        with self.assertRaisesRegex(ValueError, "no receiver given"):
            RtlSdrSource("")
        with self.assertRaisesRegex(ValueError, "no program given"):
            RtlSdrSource(program=())

    def test_chooses_its_own_address(self):
        source = RtlSdrSource(program=FAKE_RTL_TCP)     # nothing reads: nothing starts
        state = source.describe()
        self.assertIsNone(state["address"])
        self.assertEqual(state["settable"], ["hf_input", "converter_lo_hz", "converter", "sample_rate_hz",
                                             "centre_hz", "gain_db", "ppm", "driver_pll", "tuner_grid",
                                             "rtl_agc", "bias_tee"])
        with self.assertRaisesRegex(ValueError, "unknown setting: address"):
            source.configure({"address": "192.0.2.10"})

    @unittest.skipUnless(sys.platform.startswith("linux") and shutil.which("setpriv"),
                         "setpriv, on Linux")
    async def test_rtl_tcp_ends_when_radiolack_is_killed_even_one_that_ignores_sigterm(self):
        radiolack = await asyncio.create_subprocess_exec(
            sys.executable, "-c", KILLED_RADIOLACK, "stubborn", *FAKE_RTL_TCP, stdout=asyncio.subprocess.PIPE)
        try:
            line = await asyncio.wait_for(radiolack.stdout.readline(), 20)
            self.assertEqual(line, b"streaming\n")
            [(pid, _)] = self.runs.started()
        finally:
            radiolack.kill()
            await radiolack.wait()
        await eventually(lambda: not running(pid), timeout=10)


if __name__ == "__main__":
    unittest.main()
