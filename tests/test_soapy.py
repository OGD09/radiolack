"""Tests for the SoapySDR source (radiolack/soapy.py), against the stand-in
SoapySDR module (tests/soapysdr/SoapySDR.py): never a device, never the
real module, which tests/__init__.py keeps out of the test run, and never
a transmit direction, which fails the test even when the code caught the
stand-in's refusal. Its devices give samples paced by the wall clock: the
tests wait for what they check, within generous limits, and never measure
a time."""

import asyncio
import json
import logging
import math
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

from radiolack import soapy as soapy_source
from radiolack.demod import FRAME_KIND_AUDIO
from radiolack.engine import Engine
from radiolack.soapy import (FORMATS, MISSING, Capabilities, Losses, NativeBuffer, SoapySource, args_text,
                             load_module, module_problem, parse_args)
from radiolack.sources import Sources
from radiolack.store import SETTINGS_FILE, Store
from tests.fakes import STAND_IN_SOAPYSDR, stand_in_soapysdr
from tests.test_engine import eventually, next_carrier, next_frame

SOURCE_FILE = Path(soapy_source.__file__)
RATE_HZ = 1_024_000.0
TONE_STEP = 2 * math.pi / 64        # the stand-in's tone: a turn every 64 samples


class FakeClock:
    """A clock that moves only when told to."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def capabilities(**changes) -> Capabilities:
    """What a device offers: one gain stage, rates as a list, no bandwidth
    and no correction, with ``changes``."""
    values = dict(driver="test", serial="0001", hardware="test-stand-in", frequency_ranges=((1e6, 6e9),),
                  rates_hz=(RATE_HZ, 2_400_000.0), rate_ranges=(), bandwidths_hz=(), bandwidth_ranges=(),
                  gain_stage="TUNER", gain_range=(0.0, 40.0, 1.0), gain_mode=True, gain_db=10.0, correction=False,
                  corr_component=False, sample_format=FORMATS["CS8"], full_scale=128.0)
    return Capabilities(**{**values, **changes})


def is_tone(samples: np.ndarray) -> bool:
    """Whether ``samples`` hold the stand-in's tone at half full scale."""
    steps = np.angle(samples[1:] * np.conj(samples[:-1]))
    return bool(np.allclose(np.abs(samples), 0.5, atol=0.02) and np.allclose(steps, TONE_STEP, atol=0.05))


class BufferTest(unittest.TestCase):
    def test_samples_come_out_in_order(self):
        buffer = NativeBuffer(2, 10)
        buffer.put(bytes(range(6)))
        buffer.put(bytes(range(6, 10)))
        self.assertEqual(len(buffer), 5)
        self.assertEqual(buffer.take(3, 0.0), bytes(range(6)))
        self.assertEqual(buffer.take(2, 0.0), bytes(range(6, 10)))
        self.assertEqual(buffer.dropped, 0)

    def test_a_reader_behind_loses_the_oldest(self):
        buffer = NativeBuffer(4, 3)
        buffer.put(bytes(range(20)))        # five samples, two too many
        self.assertEqual(buffer.dropped, 2)
        self.assertEqual(buffer.take(3, 0.0), bytes(range(8, 20)))

    def test_a_wait_for_more_than_the_capacity_keeps_what_it_waits_for(self):
        buffer = NativeBuffer(1, 2)
        taken = []
        waiter = threading.Thread(target=lambda: taken.append(buffer.take(4, 10.0)))
        waiter.start()
        while buffer._wanted != 4:     # noqa: SLF001 - the waiter is waiting
            time.sleep(0.001)
        buffer.put(b"ab")
        buffer.put(b"cd")
        waiter.join(10.0)
        self.assertEqual(taken, [b"abcd"])
        self.assertEqual(buffer.dropped, 0)

    def test_a_take_that_waits_in_vain_gives_none(self):
        buffer = NativeBuffer(2, 4)
        buffer.put(b"ab")
        self.assertIsNone(buffer.take(2, 0.0))
        self.assertEqual(len(buffer), 1)
        with self.assertRaises(ValueError):
            buffer.take(0, 0.0)

    def test_a_take_now_keeps_room_for_what_it_asked(self):
        buffer = NativeBuffer(1, 2)
        self.assertIsNone(buffer.take_now(4))
        buffer.put(b"abcd")             # more than the capacity, kept for the reader that asked
        self.assertEqual((buffer.dropped, buffer.take_now(4)), (0, b"abcd"))
        buffer.put(b"efgh")             # nobody waits: the capacity again
        self.assertEqual((buffer.dropped, buffer.take_now(2)), (2, b"gh"))
        with self.assertRaises(ValueError):
            buffer.take_now(0)

    def test_only_whole_samples_go_in(self):
        with self.assertRaises(ValueError):
            NativeBuffer(2, 4).put(b"abc")
        with self.assertRaises(ValueError):
            NativeBuffer(0, 4)


class ArgumentsTest(unittest.TestCase):
    def test_device_arguments_name_a_driver(self):
        self.assertEqual(parse_args(" driver = rtlsdr , serial=0001,"), {"driver": "rtlsdr", "serial": "0001"})
        self.assertEqual(args_text({"serial": "0001", "driver": "sdrplay"}), "driver=sdrplay,serial=0001")
        for text, reason in (("", "no device arguments"), ("serial=0001", "must name a driver"),
                             ("driver=", "must name a driver"), ("driver", "KEY=VALUE"),
                             ("driver=a,driver=b", "given twice"), ("driver=a\nb", "printable"),
                             ("driver=" + "a" * 300, "longer than"), (None, "must be text")):
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, reason):
                parse_args(text)

    def test_a_source_needs_them_too(self):
        with self.assertRaisesRegex(ValueError, "must name a driver"):
            SoapySource("serial=0001")


class ModuleTest(unittest.TestCase):
    def test_the_real_module_stays_out_of_the_tests(self):
        # tests/__init__.py: SoapySDR is missing for the source, installed or not.
        self.assertEqual(load_module(), (None, MISSING))
        self.assertEqual(module_problem(), MISSING)
        self.assertIn("python3-soapysdr", MISSING)
        with stand_in_soapysdr() as soapy:
            module, problem = load_module()
            self.assertEqual((module, problem), (soapy, ""))
            self.assertIsNone(module_problem())

    def test_a_module_that_fails_to_load_says_why(self):
        def broken(name):
            raise OSError("libSoapySDR.so.0.8: cannot open shared object file")

        with mock.patch.object(soapy_source.importlib, "import_module", broken):
            module, problem = load_module()
        self.assertIsNone(module)
        self.assertEqual(problem, "the SoapySDR Python module cannot be loaded: libSoapySDR.so.0.8: cannot open "
                                  "shared object file")

    def test_the_source_names_no_transmit_direction(self):
        text = SOURCE_FILE.read_text(encoding="utf-8")
        for name in ("SOAPY_SDR_TX", "writeStream", "TX", "transmit_requests"):
            with self.subTest(name=name):
                self.assertNotIn(name, text.replace("SOAPY_SDR_RX", ""))

    def test_a_transmit_direction_caught_still_fails_the_test(self):
        with self.assertRaisesRegex(AssertionError, "transmit direction"):
            with stand_in_soapysdr() as soapy:
                soapy.add("test")
                device = soapy.Device("driver=test")
                for call in (lambda: device.setupStream(soapy.SOAPY_SDR_TX, "CS8"),
                             lambda: device.writeStream(None, [], 0)):
                    try:
                        call()
                    except AssertionError:
                        pass        # as code under test might


class FormatTest(unittest.TestCase):
    def test_the_native_format_without_the_bindings_call(self):
        with stand_in_soapysdr() as soapy:
            for native, formats, expected in (("CS16", ("CS16", "CF32"), "CS16"), ("CF32", ("CF32", "CS16"), "CF32"),
                                              ("CS8", ("CU8", "CS8"), "CS8"), ("CU8", ("CU8",), None)):
                with self.subTest(formats=formats):
                    soapy.reset()
                    soapy.add("test", native=native, formats=formats)
                    self.assertEqual(soapy_source.native_format(soapy, soapy.Device("driver=test")), expected)

    def test_the_drivers_full_scales_and_cf32_for_the_others(self):
        cases = (("rtlsdr", ("CS8", "CS16", "CF32"), "CS8", 128.0), ("hackrf", ("CS8", "CS16", "CF32"), "CS8", 128.0),
                 ("sdrplay", ("CS16", "CF32"), "CS16", 32767.0), ("airspy", ("CS16", "CF32"), "CS16", 32767.0),
                 ("bladerf", ("CS16", "CF32"), "CS16", 2048.0), ("lime", ("CS16", "CF32"), "CF32", 1.0),
                 ("rtlsdr", ("CS16", "CF32"), "CF32", 1.0))
        with stand_in_soapysdr() as soapy:
            for driver, formats, expected, scale in cases:
                with self.subTest(driver=driver, formats=formats):
                    soapy.reset()
                    soapy.add(driver, native=formats[0], formats=formats)
                    chosen, full_scale = soapy_source.stream_format(soapy, soapy.Device(f"driver={driver}"), driver)
                    self.assertEqual((chosen.name, full_scale), (expected, scale))

    def test_a_device_without_cf32_reads_its_own_at_its_nominal_scale(self):
        with stand_in_soapysdr() as soapy:
            soapy.add("lime", native="CS16", formats=("CS16",))
            with self.assertLogs("radiolack.soapy", logging.WARNING) as logs:
                chosen, full_scale = soapy_source.stream_format(soapy, soapy.Device("driver=lime"), "lime")
            self.assertEqual((chosen.name, full_scale), ("CS16", 32768.0))
            self.assertIn("full scale is not known", logs.output[0])
            soapy.reset()
            soapy.add("lime", native="CU8", formats=("CU8",))
            with self.assertRaisesRegex(ValueError, "none of the formats"):
                soapy_source.stream_format(soapy, soapy.Device("driver=lime"), "lime")


class CapabilitiesTest(unittest.TestCase):
    def test_rates_listed_up_to_8_ms_s(self):
        offered = capabilities(rates_hz=(250e3, RATE_HZ, 2.4e6, 8e6, 10e6))
        self.assertEqual(offered.menu_rates(), [250e3, RATE_HZ, 2.4e6, 8e6])
        self.assertTrue(offered.offers(8e6))
        self.assertFalse(offered.offers(10e6))
        self.assertFalse(offered.offers(3e6))
        self.assertEqual(offered.nearest_rate(3e6), 2.4e6)
        self.assertIn("0.25, 1.024, 2.4, 8 MS/s", offered.rates_text())

    def test_rates_within_ranges(self):
        offered = capabilities(rates_hz=(), rate_ranges=((2e6, 20e6, 0.0),))
        self.assertEqual(offered.menu_rates(), [2.048e6, 2.4e6, 3.2e6, 4e6, 5e6, 6e6, 8e6])
        self.assertTrue(offered.offers(2_222_222.0))
        self.assertFalse(offered.offers(9e6))
        self.assertEqual(offered.nearest_rate(RATE_HZ), 2e6)
        self.assertEqual(offered.nearest_rate(12e6), 8e6)
        self.assertEqual(offered.nearest_rate(2_222_222.0), 2_222_222.0)
        stepped = capabilities(rates_hz=(), rate_ranges=((1e6, 3e6, 1e6),))
        self.assertEqual(stepped.menu_rates(), [])      # no common rate on its steps
        self.assertEqual((stepped.offers(2e6), stepped.offers(2.4e6)), (True, False))
        self.assertEqual(stepped.nearest_rate(2.4e6), 2e6)
        with self.assertRaisesRegex(ValueError, "no sample rate"):
            capabilities(rates_hz=(10e6,)).nearest_rate(RATE_HZ)

    def test_the_bandwidth_at_or_below_four_fifths_of_the_rate(self):
        listed = capabilities(bandwidths_hz=(200e3, 600e3, 1.536e6, 5e6, 8e6))
        self.assertEqual(listed.bandwidth_for(2.4e6), 1.536e6)
        self.assertEqual(listed.bandwidth_for(8e6), 5e6)
        self.assertEqual(listed.bandwidth_for(240e3), 200e3)
        self.assertEqual(capabilities(bandwidths_hz=(5e6,)).bandwidth_for(RATE_HZ), 5e6)    # all wider: the narrowest
        ranged = capabilities(bandwidth_ranges=((200e3, 8e6, 1e3),))
        self.assertEqual(ranged.bandwidth_for(2.4e6), 1.92e6)
        self.assertEqual(ranged.bandwidth_for(2_222_222.0), 1_777_000.0)
        self.assertEqual(ranged.bandwidth_for(100e3), 200e3)
        self.assertIsNone(capabilities().bandwidth_for(2.4e6))

    def test_gains_of_one_stage(self):
        self.assertEqual(len(capabilities().gains_db()), 41)
        self.assertEqual(capabilities(gain_range=(0.0, 49.6, 0.0)).gains_db()[-2:], (49.0, 49.6))
        widened = capabilities(gain_range=(0.0, 100.0, 0.1)).gains_db()
        self.assertEqual((len(widened), widened[0], widened[-1]), (soapy_source.MAX_GAINS, 0.0, 100.0))
        none = capabilities(gain_stage=None, gain_range=None)
        self.assertEqual((none.gains_db(), none.gain_settable), ((), False))
        self.assertNotIn("gain_db", soapy_source.settable(none))
        self.assertIn("gain_db", soapy_source.settable(None))       # until the device is known

    def test_the_correction_the_device_takes(self):
        settings = soapy_source.Settings(145e6, RATE_HZ, None, 37.42)
        for correction, whole in ((True, 37), (False, 0)):
            with self.subTest(correction=correction):
                tuning = soapy_source.tuning(settings, capabilities(correction=correction))
                scale = (1 + 37.42e-6) / (1 + whole / 1e6)
                self.assertEqual(tuning.whole_ppm, whole)
                self.assertAlmostEqual(tuning.scale, scale, places=12)
                self.assertAlmostEqual(tuning.request_hz * scale, 145e6, delta=0.5)
                self.assertAlmostEqual(tuning.centre_hz, 145e6, delta=0.5)

    def test_changes_are_checked_against_the_device(self):
        settings = soapy_source.Settings(145e6, RATE_HZ, None, 0.0)
        offered = capabilities(frequency_ranges=((24e6, 1766e6),))
        for changes, reason in (({"sample_rate_hz": 3e6}, "must be one this device offers"),
                                ({"centre_hz": 2e9}, "tunes from 24 to 1766"),
                                ({"bias_tee": True}, "unknown setting: bias_tee"),
                                ({"ppm": 200}, "correction")):
            with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, reason):
                soapy_source.changed(settings, changes, offered)
        self.assertEqual(soapy_source.changed(settings, {"gain_db": 20.4}, offered).gain_db, 20.0)
        without = capabilities(gain_mode=False)
        with self.assertRaisesRegex(ValueError, "no automatic gain"):
            soapy_source.changed(settings, {"gain_db": None}, without)
        # A new rate brings the centre within the new reach, from half the rate.
        low = soapy_source.changed(replace_centre(settings, 600e3), {"sample_rate_hz": 2.4e6},
                                   capabilities(frequency_ranges=((0.0, 6e9),)))
        self.assertEqual(low.centre_hz, 1.2e6)
        # Before the device is known: any rate up to 8 MS/s, which its opening fits.
        unknown = soapy_source.changed(settings, {"sample_rate_hz": 3e6}, None)
        self.assertEqual(soapy_source.fitted(unknown, offered).sample_rate_hz, 2.4e6)
        with self.assertRaisesRegex(ValueError, "at most 8 MS/s"):
            soapy_source.changed(settings, {"sample_rate_hz": 10e6}, None)


def replace_centre(settings, centre_hz):
    return soapy_source.Settings(centre_hz, settings.sample_rate_hz, settings.gain_db, settings.ppm)


class LossesTest(unittest.TestCase):
    def test_logged_at_most_once_a_minute_with_the_counts_since(self):
        clock = FakeClock()
        losses = Losses("SoapySDR test", 60.0, clock)
        losses.check()      # nothing to say
        with self.assertLogs("radiolack.soapy", logging.WARNING) as logs:
            losses.add(overflows=2, dropped=1000)
            losses.check()
            clock.now += 30
            losses.add(late_blocks=3)
            losses.check()          # too soon
            clock.now += 31
            losses.add(overflows=1)
            losses.check()
            losses.add(dropped=5)
            losses.check(flush=True)
        self.assertEqual([record.getMessage() for record in logs.records], [
            "SoapySDR test: 2 overflows reported by the device, 1000 samples dropped, the engine behind, in the "
            "last 0 s",
            "SoapySDR test: 1 overflows reported by the device, 3 blocks took the engine longer than their "
            "period, in the last 61 s",
            "SoapySDR test: 5 samples dropped, the engine behind, in the last 0 s"])


class SourceCase(unittest.IsolatedAsyncioTestCase):
    """A SoapySDR source on the stand-in module, closed after each test,
    before the stand-in leaves."""

    async def asyncSetUp(self):
        self.soapy = self.enterContext(stand_in_soapysdr())
        self.history: dict[SoapySource, list[tuple[str, str]]] = {}

    def device(self, driver: str = "test", **settings):
        """A device of the stand-in's, at 1.024 MS/s and in CF32 unless
        ``settings`` say otherwise."""
        return self.soapy.add(driver, **{"rates_hz": (RATE_HZ,), "formats": ("CS8", "CF32"), **settings})

    def source(self, args: str = "driver=test", **options) -> SoapySource:
        """A source whose states, as it notifies them, go to ``history``."""
        timings = {"settle_s": 0.0, "idle_s": 30.0, "read_wait_s": 0.5, "silence_s": 1.0, "retry_s": (0.01, 0.05),
                   "stop_s": 10.0, **options}
        source = SoapySource(args, **timings)
        history: list[tuple[str, str]] = []
        source.on_change = lambda: history.append((source.status, source.detail))
        self.history[source] = history
        self.addAsyncCleanup(self.closed, source)
        return source

    async def closed(self, source: SoapySource) -> None:
        source.close()
        await asyncio.wait_for(source.wait_closed(), 15)
        self.assertFalse(source._thread is not None and source._thread.is_alive())
        for device in self.soapy.opened:
            self.assertTrue(device.closed)
            self.assertTrue(all(stream.closed and not stream.active for stream in device.streams))

    async def samples(self, source: SoapySource, count: int = 4096, timeout: float = 10.0) -> np.ndarray:
        """The next samples the source gives, once it gives some."""
        deadline = time.monotonic() + timeout
        while (samples := await source.read(count)) is None:
            if time.monotonic() > deadline:
                raise AssertionError(f"no samples: {source.status} ({source.detail})")
        return samples

    async def state(self, source: SoapySource, status: str, detail: str) -> None:
        """Return once the source has been in ``status`` with ``detail``,
        reading once to start it if nothing has; it keeps its device for
        30 s without reads."""
        if source._task is None:
            await source.read(1024)
        history = self.history[source]
        try:
            await eventually(lambda: (status, detail) in history)
        except AssertionError:
            raise AssertionError(f"never {status} ({detail}): {history}") from None


class SourceTest(SourceCase):
    async def test_reads_each_drivers_native_format_at_its_full_scale(self):
        cases = (("rtlsdr", "CS8", ("CS8", "CS16", "CF32"), None, "CS8"), ("hackrf", "CS8", ("CS8",), None, "CS8"),
                 ("sdrplay", "CS16", ("CS16", "CF32"), 32767.0, "CS16"),
                 ("airspy", "CS16", ("CS16", "CF32"), 32767.0, "CS16"),
                 ("bladerf", "CS16", ("CS16", "CF32"), 2048.0, "CS16"),
                 ("test", "CS16", ("CS16", "CF32"), 2048.0, "CF32"), ("test", "CS16", ("CS16",), None, "CS16"))
        for driver, native, formats, full_scale, expected in cases:
            with self.subTest(driver=driver, formats=formats):
                self.soapy.reset()
                self.device(driver, native=native, formats=formats, full_scale=full_scale)
                source = self.source(f"driver={driver}")
                samples = await self.samples(source)
                self.assertEqual(samples.dtype, np.complex64)
                self.assertTrue(is_tone(samples))
                self.assertEqual(self.soapy.opened[0].streams[0].format, expected)
                await self.closed(source)

    async def test_describes_the_device(self):
        self.device(serial="0007", hardware="test-tuner", frequency_range_hz=(24e6, 1766e6),
                    rates_hz=(250e3, RATE_HZ, 2.4e6, 10e6), gains={"TUNER": (0.0, 49.6, 0.0)}, gain_mode=True)
        source = self.source("driver=test,index=0")
        self.assertEqual(source.settings_key, "soapy driver=test,index=0")
        before = source.describe()
        self.assertEqual((before["driver"], before["tuner"], before["settable"]),
                         ("test", None, ["converter_lo_hz", "converter", "sample_rate_hz", "centre_hz", "gain_db",
                                         "ppm"]))
        await self.samples(source)
        state = source.describe()
        self.assertEqual(state, {
            "kind": "soapy", "status": "streaming", "detail": "", "address": None, "tuner": "test-tuner",
            "driver": "test", "centre_hz": 145e6, "centre_in_use_hz": 145e6, "sample_rate_hz": 2.4e6,
            "sample_rates_hz": [250e3, RATE_HZ, 2.4e6], "gain_db": None, "ppm": 0.0, "gains_db": state["gains_db"],
            "range_hz": [24e6, 1766e6], "hf_input": None, "hf_inputs": [], "direct": False, "rtl_agc": None,
            "bias_tee": None, "converter": "none", "converter_lo_hz": 125e6, "driver_pll": None, "tuner_grid": None,
            "settable": ["converter_lo_hz", "converter", "sample_rate_hz", "centre_hz", "gain_db", "ppm"]})
        self.assertEqual(state["gains_db"][-1], 49.6)
        # Its settings under its driver and serial number, by which it opens again.
        self.assertEqual(source.settings_key, "soapy test 0007")
        self.assertEqual(source.reopen, {"kind": "soapy", "args": "driver=test,serial=0007"})
        self.assertEqual(source.hardware, "SoapySDR test, test-tuner")
        self.assertEqual((source.grid_hz, source.pll_tuning(), source.centre_off_grid(145_000_000)),
                         (0.0, "receiver", None))
        self.assertEqual(self.soapy.searches[0], {"driver": "test", "index": "0"})
        self.assertTrue(all(search.get("driver") == "test" for search in self.soapy.searches))

    async def test_rates_within_the_devices_ranges(self):
        device = self.device(rates_hz=(), rate_ranges=((2e6, 20e6, 0.0),))
        source = self.source()
        await self.samples(source)
        self.assertEqual(source.describe()["sample_rates_hz"], [2.048e6, 2.4e6, 3.2e6, 4e6, 5e6, 6e6, 8e6])
        source.configure({"sample_rate_hz": 2_222_222})
        await eventually(lambda: self.soapy.opened[0].rate_hz == 2_222_222)
        for rate in (10e6, 1e6):
            with self.subTest(rate=rate), self.assertRaisesRegex(ValueError, "one this device offers"):
                source.configure({"sample_rate_hz": rate})
        self.assertEqual(device.rate_ranges, ((2e6, 20e6, 0.0),))

    async def test_sets_the_bandwidth_with_the_rate(self):
        for settings, rate, expected in (({"bandwidths_hz": (600e3, 1.536e6, 5e6)}, 2.4e6, 1.536e6),
                                         ({"bandwidth_ranges": ((200e3, 8e6, 1e3),)}, RATE_HZ, 819e3),
                                         ({}, 2.4e6, None)):
            with self.subTest(settings=settings):
                self.soapy.reset()
                self.device(rates_hz=(RATE_HZ, 2.4e6), **settings)
                source = self.source()
                source.configure({"sample_rate_hz": rate})
                await self.samples(source)
                self.assertEqual(self.soapy.opened[0].bandwidth_hz, expected)
                await self.closed(source)

    async def test_one_gain_stage_takes_the_gain_and_several_keep_their_own(self):
        device = self.device(gains={"TUNER": (0.0, 40.0, 1.0)}, gain_mode=True)
        source = self.source()
        await self.samples(source)
        opened = self.soapy.opened[0]
        self.assertTrue(opened.automatic_gain)
        source.configure({"gain_db": 20.3})
        self.assertEqual(source.describe()["gain_db"], 20.0)
        await eventually(lambda: opened.gain_db["TUNER"] == 20.0 and not opened.automatic_gain)
        await self.closed(source)
        # Without automatic gain, the device's own gain to start with.
        self.soapy.reset()
        self.device(gains={"TUNER": (0.0, 40.0, 1.0)})
        source = self.source()
        await self.samples(source)
        self.assertEqual(source.describe()["gain_db"], 0.0)
        with self.assertRaisesRegex(ValueError, "no automatic gain"):
            source.configure({"gain_db": None})
        await self.closed(source)
        # Several stages: their own gains, until the gains per stage come.
        self.soapy.reset()
        self.device(gains={"AMP": (0.0, 14.0, 14.0), "LNA": (0.0, 40.0, 8.0), "VGA": (0.0, 62.0, 2.0)})
        source = self.source()
        await self.samples(source)
        state = source.describe()
        self.assertEqual((state["gain_db"], state["gains_db"], "gain_db" in state["settable"]), (None, [], False))
        with self.assertRaisesRegex(ValueError, "unknown setting: gain_db"):
            source.configure({"gain_db": 20})
        self.assertEqual(self.soapy.opened[0].gain_db, {"AMP": 0.0, "LNA": 0.0, "VGA": 0.0})
        self.assertIsNotNone(device)

    async def test_the_correction_goes_to_the_device_only_where_it_takes_one(self):
        for correction, whole in (("corr", 37), ("native", 37), ("none", 0)):
            with self.subTest(correction=correction):
                self.soapy.reset()
                self.device(correction=correction)
                source = self.source()
                source.configure({"ppm": 37.42})
                await self.samples(source)
                device = self.soapy.opened[0]
                self.assertEqual(device.correction_ppm, whole)
                scale = (1 + 37.42e-6) / (1 + whole / 1e6)
                self.assertAlmostEqual(source.rate_scale, scale, places=12)
                self.assertAlmostEqual(device.frequency_hz * scale, 145e6, delta=0.5)
                self.assertAlmostEqual(source.exact_centre_hz, 145e6, delta=0.5)
                # A retune keeps the device's correction: SoapySDR's overall frequency is given its CORR.
                source.configure({"centre_hz": 433e6})
                while source.centre_hz != 433e6:
                    await self.samples(source)
                self.assertEqual(device.correction_ppm, whole)
                if correction == "corr":
                    self.assertIn(("CORR", 37.0), device.tunings)
                await self.closed(source)

    async def test_timestamps_are_not_used(self):
        for timestamps, factor in ((True, 1.0), (True, 2.0), (False, 1.0)):
            with self.subTest(timestamps=timestamps, factor=factor):
                self.soapy.reset()
                self.device(timestamps=timestamps, timestamp_factor=factor)
                source = self.source()
                self.assertTrue(is_tone(await self.samples(source)))
                self.assertTrue(is_tone(await self.samples(source)))
                await self.closed(source)

    async def test_overflows_and_time_outs_are_counted_and_the_stream_goes_on(self):
        self.device(events={2: "overflow", 3: "timeout", 4: "timeout", 6: "overflow"})
        source = self.source(report_s=0.0)
        with self.assertLogs("radiolack.soapy", logging.WARNING) as logs:
            while not any("overflows reported by the device" in line for line in logs.output):
                await self.samples(source)
        self.assertEqual(source.status, "streaming")
        # The tone, with a gap where samples were lost.
        self.assertTrue(np.allclose(np.abs(await self.samples(source)), 0.5, atol=0.02))

    async def test_samples_dropped_for_an_engine_behind_are_counted(self):
        self.device()
        source = self.source(report_s=0.0, buffer_s=0.01)
        await self.samples(source, 1024)
        with self.assertLogs("radiolack.soapy", logging.WARNING) as logs:
            while not any("samples dropped, the engine behind" in line for line in logs.output):
                await asyncio.sleep(0.05)
                await self.samples(source, 1024)

    async def test_blocks_longer_than_their_period_are_counted(self):
        self.device()
        clock = FakeClock()
        source = self.source(clock=clock)
        await self.samples(source, 1024)        # a period of 1 ms
        clock.now += 0.5                        # the engine took half a second over it
        with self.assertLogs("radiolack.soapy", logging.WARNING) as logs:
            await self.samples(source, 1024)
        self.assertIn("1 blocks took the engine longer than their period", logs.output[0])
        clock.now += 60                         # a pause, not a block
        await self.samples(source, 1024)
        self.assertEqual(source._losses.late_blocks, 0)

    async def test_an_unplugged_device_waits_and_comes_back(self):
        for unplugged, detail in (("error", "the stream failed: STREAM_ERROR"),
                                  ("timeout", "no samples: the device stopped answering")):
            with self.subTest(unplugged=unplugged):
                self.soapy.reset()
                device = self.device(unplugged_reads=unplugged)
                source = self.source()
                await self.samples(source)
                device.present = False
                await self.state(source, "waiting", detail)
                await self.state(source, "waiting", "no test device found with its serial number")
                device.present = True
                self.assertTrue(is_tone(await self.samples(source)))
                self.assertEqual(source.status, "streaming")
                self.assertEqual(len(self.soapy.opened), 2)
                self.assertEqual(self.soapy.searches[-1], {"driver": "test", "serial": "0001"})
                await self.closed(source)

    async def test_waits_for_a_device_that_cannot_open_or_stream(self):
        cases = (({"fail_open": True}, "the device cannot be opened: SoapySDR::Device::make() device not found"),
                 ({"fail_stream": True}, "the stream cannot be opened: setupStream: the device is busy"),
                 ({"native": "CU8", "formats": ("CU8",)},
                  "the device cannot be read: the device gives none of the formats CS8, CS16 and CF32"),
                 ({"rates_hz": (10e6,)}, "the device cannot be read: the device offers no sample rate up to 8 MS/s"),
                 ({"driver": "other"}, "no test device found with the arguments given"))
        for settings, detail in cases:
            with self.subTest(settings=settings):
                self.soapy.reset()
                self.device(**{"driver": "test", **settings})
                source = self.source()
                await self.state(source, "waiting", detail)
                await self.closed(source)

    async def test_waits_without_the_module(self):
        source = self.source(load=lambda: (None, MISSING))
        await self.state(source, "waiting", MISSING)
        self.assertEqual(self.soapy.searches, [])

    async def test_iq_recordings_are_refused(self):
        source = self.source()
        with self.assertRaisesRegex(ValueError, "not available yet"):
            source.tap(lambda *args: None)
        source.tap(None)

    async def test_hangs_up_once_nobody_reads(self):
        self.device()
        source = self.source(idle_s=0.2)
        await self.samples(source)
        await eventually(lambda: source.status == "idle")
        await eventually(lambda: not source._thread.is_alive())
        device = self.soapy.opened[0]
        self.assertTrue(device.closed and device.streams[0].closed)
        self.assertTrue(is_tone(await self.samples(source)))       # opened again for the next read
        self.assertEqual(len(self.soapy.opened), 2)


class CloseTest(SourceCase):
    """A close at any moment releases the device and ends the reading
    thread, a close repeated included."""

    async def test_during_a_retry(self):
        source = self.source(load=lambda: (None, MISSING), retry_s=(60.0, 60.0))
        await self.state(source, "waiting", MISSING)
        source.close()
        source.close()
        await asyncio.wait_for(source.wait_closed(), 5)
        self.assertIsNone(await source.read(1024))

    async def test_during_a_read(self):
        device = self.device(unplugged_reads="timeout")
        source = self.source(read_wait_s=30.0, silence_s=60.0)
        await self.samples(source)
        device.present = False      # reads that time out: the source's read waits
        reading = asyncio.ensure_future(source.read(1_000_000))     # more than the buffer holds
        await asyncio.sleep(0.2)
        source.close()
        self.assertIsNone(await asyncio.wait_for(reading, 5))
        await asyncio.wait_for(source.wait_closed(), 10)

    async def test_during_a_stop(self):
        for stop in ("close", "idle"):
            with self.subTest(stop=stop):
                self.soapy.reset()
                gate = threading.Event()
                self.device(stop_gate=gate)
                source = self.source(idle_s=0.2)
                await self.samples(source)
                if stop == "idle":
                    await eventually(lambda: source._stop is not None and source._stop.is_set())
                source.close()
                closing = asyncio.ensure_future(source.wait_closed())
                await asyncio.sleep(0.1)
                source.close()          # repeated while the device stops
                asyncio.get_running_loop().call_later(0.2, gate.set)
                await asyncio.wait_for(closing, 10)
                self.assertFalse(source._thread.is_alive())
                device = self.soapy.opened[0]
                self.assertTrue(device.closed)
                self.assertTrue(device.streams[0].closed)


class EngineTest(SourceCase):
    """The engine on a SoapySDR source: spectrum, audio, the carrier where
    it is on the air through a correction, the settings of each device and
    I/Q recordings refused."""

    def engine(self, source: SoapySource, **options) -> Engine:
        engine = Engine(source, **options)
        task = asyncio.ensure_future(engine.run())

        async def stop():
            engine.close()
            await asyncio.wait_for(task, timeout=10)
            await asyncio.wait_for(engine.wait_closed(), timeout=15)

        self.addAsyncCleanup(stop)
        self.stops[engine] = stop
        return engine

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.stops = {}

    async def test_spectrum_audio_and_a_carrier_through_a_correction(self):
        air_hz = 145_012_345.6
        for correction in ("corr", "none"):
            with self.subTest(correction=correction):
                self.soapy.reset()
                self.device(correction=correction, crystal_ppm=37.42, carrier_hz=air_hz)
                engine = self.engine(self.source())
                subscription = engine.subscribe()
                engine.set_audio(subscription, True)
                engine.configure({"ppm": 37.42, "centre_hz": 145e6})
                engine.configure_demod({"frequency_hz": round(air_hz), "mode": "AM"})
                await eventually(lambda: engine.state()["status"] == "streaming")
                frame = await next_frame(subscription)
                self.assertIsNotNone(frame)
                engine.measure_carrier({"action": "measure", "id": 1}, subscription)
                result = await next_carrier(subscription)
                self.assertIsNone(result["reason"])
                self.assertAlmostEqual(result["frequency_hz"], air_hz, delta=1.0)
                self.assertGreater(result["margin_db"], 30)
                audio = []
                while len(audio) < 3:
                    message = await asyncio.wait_for(subscription.get(), 10)
                    if isinstance(message, bytes) and message[0] == FRAME_KIND_AUDIO:
                        audio.append(message)
                await self.stops[engine]()      # the next device alone streams

    async def test_each_device_keeps_its_settings(self):
        directory = Path(self.enterContext(tempfile.TemporaryDirectory())) / "radiolack"
        first = self.device(serial="0001")

        async def run(serial_expected: str, **changes) -> dict:
            engine = Engine(self.source(), sources=Sources(), store=Store(directory))
            task = asyncio.ensure_future(engine.run())
            subscription = engine.subscribe()
            await eventually(lambda: engine.state()["status"] == "streaming")
            await eventually(lambda: engine.source.settings_key == f"soapy test {serial_expected}")
            state = engine.state()
            if changes:
                engine.configure(changes)
            engine.unsubscribe(subscription)
            engine.close()
            await asyncio.wait_for(task, 10)
            await asyncio.wait_for(engine.wait_closed(), 15)
            return state

        await run("0001", ppm=12.5, centre_hz=433e6)
        saved = json.loads((directory / SETTINGS_FILE).read_text(encoding="utf-8"))
        self.assertEqual(saved["source"], {"kind": "soapy", "args": "driver=test,serial=0001"})
        self.assertEqual(set(saved["receivers"]), {"soapy test 0001"})
        self.assertEqual((saved["receivers"]["soapy test 0001"]["source"]["ppm"],
                          saved["receivers"]["soapy test 0001"]["source"]["centre_hz"]), (12.5, 433e6))
        # The same device, given by its driver alone: its settings once it is found.
        state = await run("0001")
        self.assertEqual((state["ppm"], state["centre_hz"]), (12.5, 433e6))
        # Another device: its own.
        first.present = False
        self.device(serial="0002")
        state = await run("0002")
        self.assertEqual((state["ppm"], state["centre_hz"]), (0.0, 145e6))
        saved = json.loads((directory / SETTINGS_FILE).read_text(encoding="utf-8"))
        self.assertEqual(set(saved["receivers"]), {"soapy test 0001", "soapy test 0002"})

    async def test_a_scan_started_before_the_device_is_found_starts_again_from_its_settings(self):
        directory = Path(self.enterContext(tempfile.TemporaryDirectory())) / "radiolack"
        directory.mkdir()
        (directory / SETTINGS_FILE).write_text(json.dumps({
            "version": 1, "receivers": {"soapy test 0001": {"source": {"ppm": 12.5, "centre_hz": 433e6}}},
            "scanner": {"sweep_low_hz": 144e6, "sweep_high_hz": 146e6}}), encoding="utf-8")
        self.device(serial="0001")
        engine = self.engine(self.source(), store=Store(directory))
        engine.start_given_scan("sweep")
        await eventually(lambda: engine.source.settings_key == "soapy test 0001")
        self.assertEqual(engine.scanner_state()["scan"], "sweep")
        self.assertEqual(engine.state()["ppm"], 12.5)
        # The sweep's windows, from 144 MHz, not the saved centre, which comes back once it stops.
        self.assertLess(engine.state()["centre_hz"], 146e6)
        engine.configure_scanner({"scan": None})
        await eventually(lambda: engine.state()["centre_hz"] == 433e6)

    async def test_iq_recordings_are_refused_and_write_nothing(self):
        folder = Path(self.enterContext(tempfile.TemporaryDirectory())) / "recordings"
        self.device()
        engine = self.engine(self.source(), recordings=folder)
        subscription = engine.subscribe()
        await eventually(lambda: engine.state()["status"] == "streaming")
        with self.assertRaisesRegex(ValueError, "I/Q recordings of a SoapySDR device are not available yet"):
            engine.configure_recording({"iq": True}, subscription)
        self.assertIsNone(engine.recording_state()["iq"])
        self.assertEqual(list(folder.glob("*")) if folder.exists() else [], [])


class StandInTest(unittest.TestCase):
    def test_the_stand_in_is_what_the_tests_import(self):
        with stand_in_soapysdr() as soapy:
            self.assertEqual(Path(soapy.__file__).parent, STAND_IN_SOAPYSDR)


if __name__ == "__main__":
    unittest.main()
