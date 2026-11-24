"""Tests for the SoapySDR source (radiolack/soapy.py), against the stand-in
SoapySDR module (tests/soapysdr/SoapySDR.py): never a device, never the
real module, which tests/__init__.py keeps out of the test run, and never
a transmit direction, which fails the test even when the code caught the
stand-in's refusal. Its devices give samples paced by the wall clock: the
tests wait for what they check, within generous limits, and never measure
a time."""

import asyncio
import dataclasses
import json
import logging
import math
import re
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

from radiolack import recordings
from radiolack import soapy as soapy_source
from radiolack.demod import FRAME_KIND_AUDIO
from radiolack.engine import Engine, Subscription
from radiolack.recordings import meta_name
from radiolack.rtltcp import Losses
from radiolack.soapy import (FORMATS, MISSING, Capabilities, NativeBuffer, SoapySource, args_text,
                             is_antenna_power, load_module, module_problem, parse_args, read_capabilities,
                             transmit_named)
from radiolack.sources import Sources
from radiolack.store import SETTINGS_FILE, Store
from radiolack.synthetic import SyntheticBand
from tests.sigmf_check import check_sigmf
from tests.fakes import STAND_IN_SOAPYSDR, stand_in_soapysdr
from tests.test_engine import (calibrated_ppm, eventually, next_carrier, next_frame, next_recording, next_reply,
                               with_iq_file)

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
                transmit = soapy.SOAPY_SDR_TX
                for call in (lambda: device.setupStream(transmit, "CS8"),
                             lambda: device.writeStream(None, [], 0),
                             lambda: device.getGainRange(transmit, 0),
                             lambda: device.listAntennas(transmit, 0),
                             lambda: device.setAntenna(transmit, 0, "TX"),
                             lambda: device.getSettingInfo(transmit, 0),
                             lambda: device.readSetting(transmit, 0, "bias_tx"),
                             lambda: device.writeSetting(transmit, 0, "bias_tx", "true")):
                    try:
                        call()
                    except soapy.TransmitRequested:
                        pass        # as code under test might
                    else:
                        self.fail("the stand-in took a transmit direction")


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
                # The frequency asked is the one tuned: the rest of the correction labels the samples.
                self.assertEqual(tuning.request_hz, 145e6)
                self.assertAlmostEqual(tuning.centre_hz, 145e6 * scale, delta=0.01)

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
        losses = Losses("SoapySDR test", 60.0, clock, soapy_source.log)     # as a SoapySDR source makes it
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
            losses.add(dropped=1_200_000, rate_hz=2_400_000)      # with their rate: their length too
            losses.add(dropped=512_000, rate_hz=1_024_000)
            losses.check(flush=True)
        self.assertEqual([record.getMessage() for record in logs.records], [
            "SoapySDR test: 2 overflows reported by the device, 1000 samples dropped, the engine behind, in the "
            "last 0 s",
            "SoapySDR test: 1 overflows reported by the device, 3 blocks took the engine longer than their "
            "period, in the last 61 s",
            "SoapySDR test: 5 samples dropped, the engine behind, in the last 0 s",
            "SoapySDR test: 1712000 samples dropped (1.0 s), the engine behind, in the last 0 s"])

    def test_late_blocks_alone_are_not_logged_and_are_forgotten_after_a_minute(self):
        clock = FakeClock()
        losses = Losses("SoapySDR test", 60.0, clock, soapy_source.log)
        with self.assertNoLogs("radiolack.soapy", logging.WARNING):
            losses.add(late_blocks=5)
            losses.check()
            clock.now += 59
            losses.add(late_blocks=2)
            losses.check()
            losses.check(flush=True)        # a source that stops says nothing of them either
            self.assertEqual(losses.late_blocks, 7)
            clock.now += 1
            losses.check()                  # a minute without a loss: the buffer absorbed them
            self.assertEqual(losses.late_blocks, 0)
            clock.now += 30
            losses.add(late_blocks=1)
            losses.check()
        with self.assertLogs("radiolack.soapy", logging.WARNING) as logs:
            clock.now += 10
            losses.add(dropped=100)
            losses.check()
        self.assertEqual([record.getMessage() for record in logs.records], [
            "SoapySDR test: 100 samples dropped, the engine behind, 1 blocks took the engine longer than their "
            "period, in the last 40 s"])


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

    async def test_a_driver_key_written_otherwise_keeps_the_native_format(self):
        # The rtlsdr module gives "RTLSDR" as its key: the driver that the arguments name decides.
        self.device("rtlsdr", driver_key="RTLSDR", native="CS8", formats=("CS8", "CS16", "CF32"))
        source = self.source("driver=rtlsdr")
        self.assertTrue(is_tone(await self.samples(source)))
        self.assertEqual(self.soapy.opened[0].streams[0].format, "CS8")
        self.assertEqual(source.describe()["driver"], "rtlsdr")
        await self.closed(source)

    async def test_describes_the_device(self):
        self.device(serial="0007", hardware="test-tuner", frequency_range_hz=(24e6, 1766e6),
                    rates_hz=(250e3, RATE_HZ, 2.4e6, 10e6), gains={"TUNER": (0.0, 49.6, 0.0)}, gain_mode=True)
        source = self.source("driver=test,index=0")
        self.assertEqual(source.settings_key, "soapy driver=test,index=0")
        before = source.describe()
        # Until it is known, every setting, which its opening fits, and no antenna power shown.
        self.assertEqual((before["driver"], before["tuner"], before["settable"], before["bias_tee"],
                          before["automatic_gain"]),
                         ("test", None, ["converter_lo_hz", "converter", "sample_rate_hz", "centre_hz", "gain_db",
                                         "stage_gains_db", "antenna", "ppm", "device_settings", "bias_tee"], None,
                          None))
        await self.samples(source)
        state = source.describe()
        self.assertEqual(state, {
            "kind": "soapy", "status": "streaming", "detail": "", "address": None, "tuner": "test-tuner",
            "driver": "test", "centre_hz": 145e6, "centre_in_use_hz": 145e6, "sample_rate_hz": 2.4e6,
            "sample_rates_hz": [250e3, RATE_HZ, 2.4e6], "gain_db": None, "automatic_gain": True, "ppm": 0.0,
            "gains_db": state["gains_db"], "range_hz": [24e6, 1766e6], "hf_input": None, "hf_inputs": [],
            "direct": False, "rtl_agc": None,
            "bias_tee": None, "converter": "none", "converter_lo_hz": 125e6, "driver_pll": None, "tuner_grid": None,
            "gain_stages": [], "stage_gains_db": {}, "antenna": None, "antennas": [], "device_settings": {},
            "device_setting_info": [], "device_refusal": None,
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

    async def test_one_gain_stage_takes_the_gain(self):
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
        self.assertEqual((source.describe()["gain_db"], source.describe()["automatic_gain"]), (0.0, False))
        with self.assertRaisesRegex(ValueError, "no automatic gain"):
            source.configure({"gain_db": None})
        await self.closed(source)
        self.assertIsNotNone(device)

    async def test_the_first_frequency_comes_before_the_sample_rate_as_well_as_after(self):
        for correction in ("corr", "none"):
            with self.subTest(correction=correction):
                self.soapy.reset()
                self.device(correction=correction)
                source = self.source()
                await self.samples(source)
                opened = self.soapy.opened[0]
                [before_rate] = opened.rate_tunings
                self.assertEqual(opened.tunings[0], ("RF", 145e6))
                self.assertGreaterEqual(before_rate, 1)
                self.assertIn(("RF", 145e6), opened.tunings[before_rate:])
                self.assertEqual(opened.frequency_hz, 145e6)
                await self.closed(source)
        # A device that takes no frequency before its sample rate opens as well.
        self.soapy.reset()
        self.device()
        tune = self.soapy.Device.setFrequency

        def refusing(device, direction, channel, *call):
            if not device.rate_tunings:
                raise RuntimeError("setFrequency before setSampleRate")
            tune(device, direction, channel, *call)

        with mock.patch.object(self.soapy.Device, "setFrequency", refusing):
            source = self.source()
            self.assertTrue(is_tone(await self.samples(source)))
            opened = self.soapy.opened[0]
            self.assertEqual((opened.rate_tunings, opened.tunings, source.status), ([0], [("RF", 145e6)], "streaming"))
            await self.closed(source)

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
                self.assertEqual(device.frequency_hz, 145e6)
                self.assertAlmostEqual(source.exact_centre_hz, 145e6 * scale, delta=0.01)
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
        dropped = re.compile(r"[1-9]\d* samples dropped \(\d+\.\d s\), the engine behind")
        deadline = time.monotonic() + 10
        with self.assertLogs("radiolack.soapy", logging.WARNING) as logs:
            while not any(dropped.search(line) for line in logs.output):
                self.assertLess(time.monotonic(), deadline, logs.output)
                await asyncio.sleep(0.05)
                await self.samples(source, 1024)

    async def test_blocks_longer_than_their_period_are_counted(self):
        self.device()
        clock = FakeClock()
        source = self.source(clock=clock, idle_s=5.0)
        await self.samples(source, 1024)        # a period of 1 ms
        clock.now += 0.5                        # the engine took half a second over it
        with self.assertNoLogs("radiolack.soapy", logging.WARNING):     # alone, nothing was lost
            await self.samples(source, 1024)
        self.assertEqual(source._losses.late_blocks, 1)
        clock.now += 10                         # a pause, not a block
        await self.samples(source, 1024)
        self.assertEqual(source._losses.late_blocks, 1)
        source._losses.add(dropped=10)
        with self.assertLogs("radiolack.soapy", logging.WARNING) as logs:
            await self.samples(source, 1024)
        self.assertIn("1 blocks took the engine longer than their period", logs.output[0])

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
                source = self.source(read_wait_s=0.05)
                await self.state(source, "waiting", detail)
                await self.closed(source)

    async def test_waits_without_the_module(self):
        source = self.source(load=lambda: (None, MISSING), read_wait_s=0.05)
        await self.state(source, "waiting", MISSING)
        self.assertEqual(self.soapy.searches, [])

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
        source = self.source(load=lambda: (None, MISSING), retry_s=(60.0, 60.0), read_wait_s=0.05)
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


class EngineCase(SourceCase):
    """An engine on a SoapySDR source, stopped after each test."""

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


class EngineTest(EngineCase):
    """The engine on a SoapySDR source: spectrum, audio, the carrier where
    it is on the air through a correction and the settings of each
    device."""

    async def test_spectrum_audio_and_a_carrier_through_a_correction(self):
        air_hz = 145_012_345.6
        for correction in ("corr", "none"):
            with self.subTest(correction=correction):
                self.soapy.reset()
                self.device(correction=correction, crystal_ppm=37.42, carrier_hz=air_hz)
                engine = self.engine(self.source())
                subscription = engine.subscribe()
                engine.radios[0].set_audio(subscription, True)
                engine.radios[0].configure({"ppm": 37.42, "centre_hz": 145e6})
                engine.radios[0].configure_demod({"frequency_hz": round(air_hz), "mode": "AM"})
                await eventually(lambda: engine.radios[0].state()["status"] == "streaming")
                frame = await next_frame(subscription)
                self.assertIsNotNone(frame)
                engine.radios[0].measure_carrier({"action": "measure", "id": 1}, subscription)
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

    async def test_one_calibration_brings_a_carrier_home_on_a_tuner_that_moves_in_steps(self):
        # An R820T with rtl-sdr's driver moves by about 110 Hz near 111 MHz: a frequency asked
        # a few hertz off would not move it, so the frequency asked stays put and the rest of the
        # correction labels the samples; the correction that Calibrate derives then holds.
        true_hz = 111_400_000
        self.device(correction="corr", crystal_ppm=-3.3, carrier_hz=true_hz, tuning_step_hz=110.0)
        engine = self.engine(self.source())
        subscription = engine.subscribe()
        engine.radios[0].configure({"ppm": -3.01, "centre_hz": 111.2e6})
        engine.radios[0].configure_demod({"frequency_hz": true_hz, "mode": "AM"})
        await eventually(lambda: engine.radios[0].state()["status"] == "streaming")
        engine.radios[0].measure_carrier({"action": "measure", "id": 1}, subscription)
        before = await next_carrier(subscription)
        self.assertGreater(abs(before["frequency_hz"] - true_hz), 10.0)
        device = self.soapy.opened[0]
        asked_hz = device.frequency_hz
        # Applied as the dialog does.
        engine.radios[0].configure({"ppm": calibrated_ppm(-3.01, before["frequency_hz"], true_hz)})
        engine.radios[0].measure_carrier({"action": "measure", "id": 2}, subscription)
        after = await next_carrier(subscription)
        self.assertAlmostEqual(after["frequency_hz"], true_hz, delta=1.0)
        self.assertEqual(device.frequency_hz, asked_hz)
        await self.stops[engine]()

    async def test_each_device_keeps_its_settings(self):
        directory = Path(self.enterContext(tempfile.TemporaryDirectory())) / "radiolack"
        first = self.device(serial="0001")

        async def run(serial_expected: str, **changes) -> dict:
            engine = Engine(self.source(), sources=Sources(), store=Store(directory))
            task = asyncio.ensure_future(engine.run())
            subscription = engine.subscribe()
            await eventually(lambda: engine.radios[0].state()["status"] == "streaming")
            await eventually(lambda: engine.radios[0].source.settings_key == f"soapy test {serial_expected}")
            state = engine.radios[0].state()
            if changes:
                engine.radios[0].configure(changes)
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
        engine.radios[0].start_given_scan("sweep")
        # The reading thread learns the serial number before the loop
        # applies the device's settings; "streaming" is posted after them.
        await eventually(lambda: engine.radios[0].state()["status"] == "streaming")
        await eventually(lambda: engine.radios[0].source.settings_key == "soapy test 0001")
        self.assertEqual(engine.radios[0].scanner_state()["scan"], "sweep")
        self.assertEqual(engine.radios[0].state()["ppm"], 12.5)
        # The sweep's windows, from 144 MHz, not the saved centre, which comes back once it stops.
        self.assertLess(engine.radios[0].state()["centre_hz"], 146e6)
        engine.radios[0].configure_scanner({"scan": None})
        await eventually(lambda: engine.radios[0].state()["centre_hz"] == 433e6)

    async def test_a_devices_controls_are_saved_and_its_antenna_power_never(self):
        directory = Path(self.enterContext(tempfile.TemporaryDirectory())) / "radiolack"
        self.soapy.rspdx_r2()
        key = "soapy sdrplay 2305012345"

        async def run(**changes) -> tuple[dict, object]:
            engine = Engine(self.source("driver=sdrplay"), sources=Sources(), store=Store(directory))
            task = asyncio.ensure_future(engine.run())
            subscription = engine.subscribe()
            await eventually(lambda: engine.radios[0].state()["status"] == "streaming")
            await eventually(lambda: engine.radios[0].source.settings_key == key)
            device = self.soapy.opened[-1]
            if changes:
                engine.radios[0].configure(changes)
                await eventually(lambda: device.antenna == changes["antenna"])
            state = engine.radios[0].state()
            remote = engine.radios[0].state(remote=True)["settable"]
            engine.unsubscribe(subscription)
            engine.close()
            await asyncio.wait_for(task, 10)
            await asyncio.wait_for(engine.wait_closed(), 15)
            return {**state, "remote": remote}, device

        state, _ = await run(antenna="Antenna B", stage_gains_db={"RFGR": 6}, gain_db=None,
                             device_settings={"rfnotch_ctrl": True}, bias_tee=True)
        # A remote client may change them all, the antenna power as on this computer.
        for name in ("gain_db", "stage_gains_db", "antenna", "device_settings", "bias_tee"):
            self.assertIn(name, state["remote"])
        saved = json.loads((directory / SETTINGS_FILE).read_text(encoding="utf-8"))["receivers"][key]["source"]
        self.assertEqual((saved["antenna"], saved["stage_gains_db"], saved["device_settings"]["rfnotch_ctrl"]),
                         ("Antenna B", {"IFGR": 40.0, "RFGR": 6.0}, True))
        self.assertNotIn("bias_tee", saved)
        state, device = await run()
        self.assertEqual((state["antenna"], state["stage_gains_db"]["RFGR"], state["device_settings"]["rfnotch_ctrl"],
                          state["bias_tee"]), ("Antenna B", 6.0, True, False))
        await eventually(lambda: (device.antenna, device.gain_db["RFGR"], device.setting_values["rfnotch_ctrl"])
                         == ("Antenna B", 6.0, "true"))
        self.assertEqual(device.setting_values["biasT_ctrl"], "false")

    async def test_a_saved_file_never_turns_the_antenna_power_on(self):
        directory = Path(self.enterContext(tempfile.TemporaryDirectory())) / "radiolack"
        directory.mkdir()
        (directory / SETTINGS_FILE).write_text(json.dumps({
            "version": 1, "receivers": {"soapy rtlsdr 00000001": {"source": {
                "bias_tee": True, "device_settings": {"biastee": True, "offset_tune": True}}}}}), encoding="utf-8")
        spec = self.soapy.rtl_sdr()
        spec.settings = tuple(dataclasses.replace(setting, value="true") if setting.key == "biastee" else setting
                              for setting in spec.settings)
        engine = self.engine(self.source("driver=rtlsdr"), store=Store(directory))
        engine.subscribe()
        await eventually(lambda: engine.radios[0].source.settings_key == "soapy rtlsdr 00000001")
        device = self.soapy.opened[-1]
        # Restored in the same pass that would write the antenna power.
        await eventually(lambda: "offset_tune=true" in device.events)
        self.assertEqual(device.setting_values["biastee"], "false")
        self.assertNotIn("biastee=true", device.events)
        self.assertFalse(engine.radios[0].state()["bias_tee"])


def encoded(soapy, sample_format: str, start: int, count: int, full_scale: float | None = None) -> bytes:
    """The bytes that a stand-in device gives in ``sample_format`` for its
    samples from index ``start``, an integer format at ``full_scale`` if
    given."""
    buffer = np.empty(2 * count, FORMATS[sample_format].component)
    soapy._fill(buffer, sample_format, soapy.samples(start, count), full_scale)
    return buffer.tobytes()


def swapped(data: bytes, sample_format: str) -> bytes:
    """``data`` with I and Q swapped in each pair."""
    return np.frombuffer(data, FORMATS[sample_format].component).reshape(-1, 2)[:, ::-1].tobytes()


def holds_the_devices_samples(soapy, segment: bytes, sample_format: str, full_scale: float | None = None,
                              inverted: bool = False) -> bool:
    """Whether ``segment`` holds a stand-in device's samples one after the
    other, bit for bit, in ``sample_format``, I and Q swapped if
    ``inverted``: its tone, from some point of its period."""
    pair = FORMATS[sample_format].sample_bytes
    count = len(segment) // pair
    if not count or len(segment) % pair:
        return False
    for start in range(round(1 / soapy.TONE_CYCLES)):
        expected = encoded(soapy, sample_format, start, 1, full_scale)
        if (swapped(expected, sample_format) if inverted else expected) != segment[:pair]:
            continue
        expected = encoded(soapy, sample_format, start, count, full_scale)
        if (swapped(expected, sample_format) if inverted else expected) == segment:
            return True
    return False


def iq_bytes(state: dict) -> int:
    """How much the I/Q recording that runs was given; -1 when none runs."""
    return state["iq"]["bytes"] if state["iq"] is not None else -1


class TapTest(SourceCase):
    """What the source hands an I/Q recording's tap: the samples it keeps,
    as the device gives them, with their label, the format and the gaps."""

    async def test_the_tap_gets_every_sample_kept_with_its_gaps(self):
        self.device("bladerf", native="CS16", formats=("CS16", "CF32"), full_scale=2048.0, events={30: "overflow"})
        source = self.source("driver=bladerf")
        handed = []
        source.tap(lambda data, centre_hz, rate_hz, joined:
                   handed.append((data, centre_hz, rate_hz, joined, source.raw_format)))
        await self.samples(source)
        while self.soapy.opened[0].streams[0].reads <= 32:
            await self.samples(source)
        # A read that finds samples waiting returns without yielding, so the
        # samples kept after the overflow may still wait for the event loop:
        # stopping the tap now would drop them.
        await eventually(lambda: sum(not entry[3] for entry in handed) >= 2)
        source.tap(None)
        count = len(handed)
        await self.samples(source)
        self.assertEqual(len(handed), count)        # none after the tap's end
        order = "_le" if sys.byteorder == "little" else "_be"
        self.assertEqual({(centre_hz, rate_hz, raw_format) for _, centre_hz, rate_hz, _, raw_format in handed},
                         {(145e6, RATE_HZ, (f"ci16{order}", 2048.0))})
        # The first after the tap's start, then after the overflow: two runs of samples one after the other.
        self.assertEqual([index for index, entry in enumerate(handed) if not entry[3]][0], 0)
        runs = []
        for data, _, _, joined, _ in handed:
            if joined:
                runs[-1] += data
            else:
                runs.append(data)
        self.assertEqual(len(runs), 2)
        for run in runs:
            self.assertTrue(holds_the_devices_samples(self.soapy, run, "CS16", 2048.0))

    async def test_samples_that_wait_too_long_for_the_event_loop_are_not_recorded(self):
        source = self.source(report_s=0.0)
        source._loop = asyncio.get_running_loop()
        handed = []
        source.tap(lambda data, centre_hz, rate_hz, joined: handed.append((data, joined, source.raw_format)))
        source._label = soapy_source._Label(145e6, 1000.0, 145e6, 1.0, False, FORMATS["CS8"], 128.0)
        # As the reading thread does while the event loop is busy: 100 samples at a time, a tenth of a second.
        with mock.patch.object(soapy_source, "TAP_S", 0.25):        # 250 samples wait at most
            for index in range(4):
                source._hand(memoryview(bytes([index]) * 200))
        self.assertEqual(source._losses.unrecorded, 200)
        await eventually(lambda: len(handed) == 2)
        self.assertEqual([(data[0], joined, raw_format) for data, joined, raw_format in handed],
                         [(0, False, ("ci8", None)), (1, True, ("ci8", None))])
        # The next follows a gap; a mirrored band comes with I and Q swapped, the format's own size.
        source._label = soapy_source._Label(145e6, 1000.0, 145e6, 1.0, True, FORMATS["CS16"], 32768.0)
        source._hand(memoryview(np.array([1, 2, 3, 4], np.int16).tobytes()))
        await eventually(lambda: len(handed) == 3)
        order = "_le" if sys.byteorder == "little" else "_be"
        self.assertEqual(handed[2], (np.array([2, 1, 4, 3], np.int16).tobytes(), False, (f"ci16{order}", None)))
        with self.assertLogs("radiolack.soapy", logging.WARNING) as logs:
            source._losses.check(flush=True)
        self.assertIn("200 samples not recorded, the event loop behind", logs.output[0])

    async def test_a_tap_that_fails_is_left(self):
        self.device()
        source = self.source()

        def fail(*_args):
            raise RuntimeError("broken")

        source.tap(fail)
        with self.assertLogs("radiolack.soapy", logging.ERROR):
            await self.samples(source)
            await eventually(lambda: source._tap is None)
        self.assertTrue(is_tone(await self.samples(source)))       # the source goes on


class IQRecordingTest(EngineCase):
    """I/Q recordings of a SoapySDR device (later-milestones.md, section 1,
    pull request 5), from the stand-in into temporary folders: each format
    as the device gives it, bit for bit, with metadata that SigMF reads; a
    capture for each retune and each gap; ended by a new rate, another
    receiver, the disk or the engine's close; listed, downloaded and
    deleted."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.folder = Path(self.enterContext(tempfile.TemporaryDirectory())) / "recordings"

    async def streaming(self, engine: Engine) -> Subscription:
        """A client of ``engine``, once the device streams: a recording
        started earlier would take the rate asked before the device opens,
        which its opening may change."""
        subscription = engine.subscribe()
        await eventually(lambda: engine.radios[0].state()["status"] == "streaming")
        return subscription

    async def record(self, engine: Engine, subscription: Subscription, at_least: int = 50_000) -> dict:
        """The I/Q recording started, once it has ``at_least`` bytes."""
        engine.radios[0].configure_recording({"iq": True})
        state = await next_recording(subscription,
                                     lambda state: with_iq_file(state) and state["iq"]["bytes"] >= at_least)
        return state["iq"]

    def recorded(self, name: str, pair_bytes: int) -> tuple[dict, list[bytes]]:
        """The metadata of the recording whose data file is ``name``, checked
        against SigMF, and the samples of each of its captures."""
        data = (self.folder / name).read_bytes()
        metadata = check_sigmf(self, self.folder / name, self.folder / meta_name(name))
        starts = [capture["core:sample_start"] for capture in metadata["captures"]] + [len(data) // pair_bytes]
        return metadata, [data[pair_bytes * start:pair_bytes * end] for start, end in zip(starts, starts[1:])]

    async def test_records_each_format_as_the_device_gives_it(self):
        order = "_le" if sys.byteorder == "little" else "_be"
        cases = (("rtlsdr", "CS8", ("CS8", "CS16", "CF32"), None, "CS8", "ci8", None),
                 ("sdrplay", "CS16", ("CS16", "CF32"), 32767.0, "CS16", f"ci16{order}", 32767),
                 ("bladerf", "CS16", ("CS16", "CF32"), 2048.0, "CS16", f"ci16{order}", 2048),
                 ("test", "CS16", ("CS16", "CF32"), 2048.0, "CF32", f"cf32{order}", None))
        for driver, native, formats, full_scale, read, datatype, noted in cases:
            with self.subTest(driver=driver):
                self.soapy.reset()
                self.device(driver, native=native, formats=formats, full_scale=full_scale, hardware="test-tuner")
                engine = self.engine(self.source(f"driver={driver}"), recordings=self.folder)
                subscription = await self.streaming(engine)
                iq = await self.record(engine, subscription)
                self.assertRegex(iq["file"], r"Z_145\.000000MHz_1\.024000MSps(-\d)?\.sigmf-data$")
                pair_bytes = FORMATS[read].sample_bytes
                self.assertAlmostEqual(iq["seconds"], iq["bytes"] / pair_bytes / RATE_HZ, delta=0.05)
                await self.stops[engine]()
                metadata, segments = self.recorded(iq["file"], pair_bytes)
                described = metadata["global"]
                self.assertEqual((described["core:datatype"], described["core:sample_rate"], described["core:hw"]),
                                 (datatype, 1_024_000, f"SoapySDR {driver}, test-tuner"))
                # The full scale where it is not the format's, never applied to the samples.
                self.assertEqual(described.get("radiolack:full_scale"), noted)
                self.assertEqual(described.get("core:extensions"),
                                 None if noted is None else [{"name": "radiolack", "version": "1.0.0",
                                                              "optional": True}])
                self.assertEqual({capture["core:frequency"] for capture in metadata["captures"]}, {145_000_000})
                for segment in segments:
                    self.assertTrue(holds_the_devices_samples(self.soapy, segment, read, full_scale))

    async def test_a_retune_adds_a_capture_and_a_new_rate_or_another_receiver_ends_it(self):
        self.device(rates_hz=(RATE_HZ, 2_048_000.0))
        engine = self.engine(self.source(), recordings=self.folder, sources=Sources())
        subscription = await self.streaming(engine)
        iq = await self.record(engine, subscription)
        self.assertEqual(iq["rate_hz"], 2_048_000)      # the offered rate nearest the default 2.4 MS/s
        engine.radios[0].configure({"centre_hz": 433_000_000})
        await eventually(lambda: engine.radios[0].source.centre_hz == 433e6)
        given = iq_bytes(engine.radios[0].recording_state())
        await eventually(lambda: iq_bytes(engine.radios[0].recording_state()) > given + 100_000)
        with self.assertLogs("radiolack", logging.WARNING):
            engine.radios[0].configure({"sample_rate_hz": RATE_HZ})
        self.assertEqual(engine.radios[0].recording_state()["stopped"], {"kind": "iq", "reason": "sample_rate", "detail": ""})
        await eventually(lambda: engine.radios[0].source.sample_rate_hz == RATE_HZ)
        again = await self.record(engine, subscription)
        self.assertEqual(again["rate_hz"], 1_024_000)
        with self.assertLogs("radiolack", logging.WARNING):
            engine.radios[0].configure_devices({"args": "driver=other"})
        state = engine.radios[0].recording_state()
        self.assertEqual((state["iq"], state["stopped"]), (None, {"kind": "iq", "reason": "receiver", "detail": ""}))
        await self.stops[engine]()
        metadata, segments = self.recorded(iq["file"], 8)
        self.assertEqual([capture["core:frequency"] for capture in metadata["captures"]], [145_000_000, 433_000_000])
        for segment in segments:
            self.assertTrue(holds_the_devices_samples(self.soapy, segment, "CF32"))
        metadata, _ = self.recorded(again["file"], 8)
        self.assertEqual(metadata["global"]["core:sample_rate"], 1_024_000)

    async def test_through_a_converter_the_frequencies_on_the_air_and_a_mirrored_band_swapped(self):
        cases = (({"converter": "up", "converter_lo_hz": 125_000_000, "centre_hz": 7_100_000}, 7_100_000,
                  "an upconverter with its oscillator at 125 MHz", False),
                 ({"converter": "down_inverted", "converter_lo_hz": 5_150_000_000, "centre_hz": 3_800_000_000},
                  3_800_000_000, "a downconverter that inverts the spectrum with its oscillator at 5150 MHz", True))
        for changes, frequency, converter, inverted in cases:
            with self.subTest(converter=changes["converter"]):
                self.soapy.reset()
                self.device()
                engine = self.engine(self.source(), recordings=self.folder)
                engine.radios[0].configure(changes)
                subscription = await self.streaming(engine)
                await eventually(lambda: engine.radios[0].source.centre_hz == changes["centre_hz"])
                iq = await self.record(engine, subscription)
                self.assertRegex(iq["file"], rf"Z_{frequency / 1e6:.6f}MHz_")
                await self.stops[engine]()
                metadata, segments = self.recorded(iq["file"], 8)
                self.assertEqual(metadata["global"]["core:hw"], f"SoapySDR test, test-stand-in, behind {converter}")
                self.assertEqual({capture["core:frequency"] for capture in metadata["captures"]}, {frequency})
                for segment in segments:
                    self.assertTrue(holds_the_devices_samples(self.soapy, segment, "CF32", inverted=inverted))
                    self.assertFalse(holds_the_devices_samples(self.soapy, segment, "CF32", inverted=not inverted))

    async def test_a_disk_behind_or_short_of_space_ends_it_its_files_whole(self):
        self.device()
        engine = self.engine(self.source(), recordings=self.folder)
        subscription = await self.streaming(engine)
        with mock.patch.object(recordings, "IQ_PENDING_LIMIT_BYTES", 1), \
                mock.patch.object(recordings, "IQ_PENDING_S", 0.0), \
                self.assertLogs("radiolack", logging.WARNING):
            engine.radios[0].configure_recording({"iq": True})
            state = await next_recording(subscription, lambda state: state["stopped"] is not None)
        self.assertEqual((state["iq"], state["stopped"]),
                         (None, {"kind": "iq", "reason": "disk", "detail": "the disk did not keep up"}))
        iq = await self.record(engine, subscription)
        with mock.patch.object(recordings, "MIN_FREE_BYTES", 2 ** 62), \
                self.assertLogs("radiolack", logging.WARNING) as logs:
            state = await next_recording(subscription, lambda state: state["iq"] is None)
        self.assertEqual(state["stopped"], {"kind": "iq", "reason": "space", "detail": ""})
        # Among the warnings: a slow runner may log late blocks first.
        self.assertTrue(any("less than 2 GiB is free on its disk" in line for line in logs.output), logs.output)
        await self.stops[engine]()
        _, segments = self.recorded(iq["file"], 8)
        self.assertGreaterEqual(sum(map(len, segments)), iq["bytes"] // 2)
        for segment in segments:
            self.assertTrue(holds_the_devices_samples(self.soapy, segment, "CF32"))

    async def test_a_device_unplugged_meanwhile_leaves_a_gap_and_the_recording_goes_on(self):
        device = self.device()
        source = self.source()
        engine = self.engine(source, recordings=self.folder)
        subscription = await self.streaming(engine)
        iq = await self.record(engine, subscription)
        device.present = False
        await eventually(lambda: source.status == "waiting")
        given = iq_bytes(engine.radios[0].recording_state())
        self.assertGreaterEqual(given, iq["bytes"])         # it goes on, waiting
        device.present = True
        await eventually(lambda: iq_bytes(engine.radios[0].recording_state()) > given + 100_000)
        engine.radios[0].configure_recording({"iq": False})
        await self.stops[engine]()
        metadata, segments = self.recorded(iq["file"], 8)
        self.assertGreaterEqual(len(metadata["captures"]), 2)
        self.assertEqual({capture["core:frequency"] for capture in metadata["captures"]}, {145_000_000})
        for segment in segments:
            self.assertTrue(holds_the_devices_samples(self.soapy, segment, "CF32"))

    async def test_a_close_while_it_records_leaves_its_files_whole(self):
        self.device()
        engine = self.engine(self.source(), recordings=self.folder)
        subscription = await self.streaming(engine)
        iq = await self.record(engine, subscription, at_least=100_000)
        await self.stops[engine]()
        self.assertGreaterEqual((self.folder / iq["file"]).stat().st_size, iq["bytes"])
        metadata, segments = self.recorded(iq["file"], 8)
        self.assertEqual(len(metadata["captures"]), 1)
        self.assertTrue(holds_the_devices_samples(self.soapy, segments[0], "CF32"))

    async def test_lists_downloads_and_deletes_it(self):
        self.device("sdrplay", native="CS16", formats=("CS16", "CF32"), full_scale=32767.0)
        engine = self.engine(self.source("driver=sdrplay"), recordings=self.folder)
        subscription = await self.streaming(engine)
        name = (await self.record(engine, subscription))["file"]
        meta = meta_name(name)
        engine.radios[0].configure_recording({"iq": False})
        await eventually(lambda: not engine.unfinished)        # its thread done
        engine.configure_recordings({}, subscription)
        [entry] = (await next_reply(subscription, "recordings"))["files"]
        size = (self.folder / name).stat().st_size
        self.assertEqual((entry["name"], entry["kind"], entry["meta"], entry["rate_hz"], entry["running"]),
                         (name, "iq", meta, 1_024_000, False))
        self.assertEqual((entry["bytes"], entry["seconds"]),
                         (size + (self.folder / meta).stat().st_size, round(size // 4 / RATE_HZ, 1)))
        for file in (name, meta):
            self.assertEqual(engine.download_folder(file), self.folder)
        engine.configure_recordings({"delete": name}, subscription)
        self.assertEqual((await next_reply(subscription, "recordings"))["files"], [])
        self.assertEqual(list(self.folder.iterdir()), [])

    async def test_never_on_the_simulated_band_nor_at_start(self):
        engine = self.engine(SyntheticBand(), recordings=self.folder, sources=Sources())
        with self.assertRaisesRegex(ValueError, "^the simulated band is not recorded$"):
            engine.radios[0].configure_recording({"iq": True})
        self.device()
        engine.radios[0].configure_devices({"args": "driver=test"})
        await self.streaming(engine)
        self.assertEqual((engine.radios[0].recording_state()["iq"], self.folder.exists()), (None, False))


def opened_capabilities(soapy, driver: str) -> Capabilities:
    """What the stand-in's ``driver`` device offers, read as the source
    reads it."""
    return read_capabilities(soapy, soapy.Device({"driver": driver}), {"driver": driver})


def plain(soapy, driver: str = "test", **settings):
    """A stand-in device of ``driver`` with ``settings``."""
    return soapy.add(driver, **{"rates_hz": (RATE_HZ,), "formats": ("CS8", "CF32"), **settings})


class DeviceControlsTest(unittest.TestCase):
    """What the source reads of a device's gain stages, antenna sockets and
    settings, and how it checks the changes asked of them."""

    def test_antenna_power_and_transmission_by_key_or_name(self):
        for key, name in (("biastee", "Bias Tee"), ("bias_tx", "Antenna Bias"), ("biasT_ctrl", "Bias-T Enable"),
                          ("ant_power", ""), ("x", "Antenna power"), ("BIAS", "")):
            with self.subTest(key=key):
                self.assertTrue(is_antenna_power(key, name))
        for key, name in (("rfnotch_ctrl", "RF Notch Filter"), ("agc_setpoint", "AGC Setpoint"),
                          ("direct_samp", "Direct Sampling"), ("antenna", "Antenna")):
            with self.subTest(key=key):
                self.assertFalse(is_antenna_power(key, name))
        self.assertTrue(transmit_named("tx_gain", ""))
        self.assertTrue(transmit_named("mode", "Transmit mode"))
        self.assertTrue(transmit_named("bias_tx", "Antenna Bias"))
        self.assertFalse(transmit_named("iq_swap", "I/Q Swap"))

    def test_a_hackrf_one(self):
        with stand_in_soapysdr() as soapy:
            soapy.hackrf_one()
            offered = opened_capabilities(soapy, "hackrf")
        self.assertEqual([(stage.name, stage.span, stage.held) for stage in offered.stages],
                         [("LNA", (0.0, 40.0, 8.0), False), ("AMP", (0.0, 14.0, 14.0), True),
                          ("VGA", (0.0, 62.0, 2.0), False)])
        self.assertEqual((offered.gain_stage, offered.gain_mode, offered.overall_range),
                         (None, False, (0.0, 116.0, 0.0)))
        self.assertEqual(dict(offered.stage_gains_db), {"LNA": 16.0, "AMP": 14.0, "VGA": 16.0})
        # The overall gains with the amplifier off, and on.
        off, on = offered.overall_gains_db((("AMP", 0.0),)), offered.overall_gains_db((("AMP", 14.0),))
        self.assertEqual((off[0], off[-1], on[0], on[-1]), (0.0, 102.0, 14.0, 116.0))
        # One socket: no choice; its antenna power, which is no setting shown.
        self.assertEqual((offered.antennas, offered.settings), ((), ()))
        self.assertEqual(offered.power, (soapy_source.PowerSetting("bias_tx", False, "false", "true"),))
        self.assertEqual(soapy_source.settable(offered),
                         ["converter_lo_hz", "converter", "sample_rate_hz", "centre_hz", "gain_db",
                          "stage_gains_db", "ppm", "bias_tee"])

    def test_an_rspdx_r2(self):
        with stand_in_soapysdr() as soapy:
            soapy.rspdx_r2()
            offered = opened_capabilities(soapy, "sdrplay")
        self.assertEqual([stage.name for stage in offered.stages], ["IFGR", "RFGR"])
        self.assertTrue(offered.gain_mode)
        self.assertEqual((offered.antennas, offered.antenna), (("Antenna A", "Antenna B", "Antenna C"), "Antenna A"))
        self.assertEqual([setting.key for setting in offered.settings],
                         ["rfnotch_ctrl", "dabnotch_ctrl", "hdr_ctrl", "iqcorr_ctrl", "agc_setpoint"])
        self.assertEqual(dict(offered.setting_values), {"rfnotch_ctrl": False, "dabnotch_ctrl": False,
                                                        "hdr_ctrl": False, "iqcorr_ctrl": True, "agc_setpoint": -30})
        setpoint = offered.setting("agc_setpoint")
        self.assertEqual(setpoint.describe(), {"key": "agc_setpoint", "name": "AGC Setpoint",
                                               "description": "AGC Setpoint (dBfs)", "type": "number", "options": [],
                                               "range": [-60.0, 0.0, 1.0], "integer": True})
        self.assertEqual([setting.key for setting in offered.power], ["biasT_ctrl"])

    def test_an_rtl_sdr(self):
        with stand_in_soapysdr() as soapy:
            soapy.rtl_sdr()
            offered = opened_capabilities(soapy, "rtlsdr")
        self.assertEqual((offered.driver, offered.gain_stage, offered.stages, offered.antennas),
                         ("rtlsdr", "TUNER", (), ()))
        direct = offered.setting("direct_samp")
        self.assertEqual((direct.kind, direct.options), ("choice", (("0", "Off"), ("1", "I-ADC"), ("2", "Q-ADC"))))
        self.assertEqual(dict(offered.setting_values),
                         {"direct_samp": "0", "offset_tune": False, "iq_swap": False, "digital_agc": False})
        self.assertEqual(offered.power, (soapy_source.PowerSetting("biastee", False, "false", "true"),))
        # Options without names: named by their values.
        with stand_in_soapysdr() as soapy:
            spec = soapy.rtl_sdr()
            spec.settings = tuple(dataclasses.replace(setting, option_names=()) for setting in spec.settings)
            direct = opened_capabilities(soapy, "rtlsdr").setting("direct_samp")
        self.assertEqual(direct.options, (("0", "0"), ("1", "1"), ("2", "2")))

    def test_what_a_device_declares_is_bounded_and_kept_as_text(self):
        with stand_in_soapysdr() as soapy:
            spec, info = soapy.SettingSpec, soapy.ArgInfo
            plain(soapy, settings=(
                spec("odd", "false", "<b>Bold</b>\n\x07name", "x" * 400),
                spec("free", "abc", "Free text", type=info.STRING),
                spec("loose", "1", "No range", type=info.FLOAT),
                spec("bad_option", "a", "Bad option", type=info.STRING, options=("a", "b\nc")),
                spec(" spaced", "false", "Spaced key"),
                spec("level", "2", "Level", type=info.INT, range=(0.0, 10.0, 1.0), channel=True)),
                antennas=(*(f"A{index}" for index in range(20)), "A1"),
                gains={f"S{index}": (0.0, 10.0, 1.0) for index in range(10)})
            plain(soapy, "many", settings=tuple(spec(f"box{index}", "false", f"Box {index}") for index in range(40)))
            offered = opened_capabilities(soapy, "test")
            many = opened_capabilities(soapy, "many")
        self.assertEqual([setting.key for setting in offered.settings], ["odd", "level"])
        odd = offered.setting("odd")
        self.assertEqual(odd.name, "<b>Bold</b> name")      # text, which the interface shows as text
        self.assertEqual((len(odd.description), odd.description[-1]), (soapy_source.MAX_DESCRIPTION_CHARS, "…"))
        self.assertTrue(offered.setting("level").channel)
        self.assertEqual(len(offered.antennas), soapy_source.MAX_ANTENNAS)
        self.assertEqual(len(offered.stages), soapy_source.MAX_STAGES)
        self.assertEqual(len(many.settings), soapy_source.MAX_DEVICE_SETTINGS)

    def test_antenna_power_values_and_several_settings(self):
        with stand_in_soapysdr() as soapy, self.assertLogs("radiolack.soapy", logging.INFO) as logs:
            spec, info = soapy.SettingSpec, soapy.ArgInfo
            plain(soapy, settings=(
                spec("biastee", "true", "Bias Tee"),
                spec("ant_power", "On", "Antenna power", type=info.STRING, options=("Off", "On")),
                spec("bias_level", "1", "Bias level", type=info.INT, range=(1.0, 5.0, 1.0)),
                spec("bias_mode", "x", "Bias mode", type=info.STRING),
                spec("bias_tx", "1", "Antenna Bias", type=info.INT, range=(0.0, 5.0, 1.0), channel=True)))
            offered = opened_capabilities(soapy, "test")
        # The channel's first, then the device's in their order; none shown as a setting.
        self.assertEqual(offered.power, (soapy_source.PowerSetting("bias_tx", True, "0", "1"),
                                         soapy_source.PowerSetting("biastee", False, "false", "true"),
                                         soapy_source.PowerSetting("ant_power", False, "Off", "On")))
        self.assertEqual(offered.driven_power.key, "bias_tx")
        self.assertEqual(offered.settings, ())
        messages = [record.getMessage() for record in logs.records]
        self.assertIn("the antenna power 'bias_level' is left as it is: its off value cannot be told", messages)
        self.assertIn("the antenna power 'bias_mode' is left as it is: its off value cannot be told", messages)
        self.assertIn("antenna power: bias_tx follows the antenna power, biastee, ant_power stay off", messages)

    def test_changes_to_the_stages_antenna_and_settings_are_checked(self):
        with stand_in_soapysdr() as soapy:
            soapy.rspdx_r2()
            offered = opened_capabilities(soapy, "sdrplay")
        settings = soapy_source.fitted(soapy_source.Settings(145e6, 2e6, None, 0.0), offered)
        changed = soapy_source.changed(settings, {"stage_gains_db": {"RFGR": 6.4, "OTHER": 3},
                                                  "antenna": "Antenna B",
                                                  "device_settings": {"agc_setpoint": -40.2, "gone": True}}, offered)
        self.assertEqual((dict(changed.stage_gains), changed.antenna, dict(changed.device_settings)["agc_setpoint"]),
                         ({"IFGR": 40.0, "RFGR": 6.0}, "Antenna B", -40))
        self.assertNotIn("gone", dict(changed.device_settings))
        for changes, reason in (({"stage_gains_db": [1]}, "stages' gains must be an object"),
                                ({"stage_gains_db": {"RFGR": "loud"}}, "the gain of RFGR"),
                                ({"antenna": "Antenna D"}, "the antenna must be one of: Antenna A, Antenna B"),
                                ({"antenna": 3}, "the antenna must be one of the device's"),
                                ({"device_settings": {"hdr_ctrl": "yes"}}, "HDR Mode must be true or false"),
                                ({"device_settings": {"agc_setpoint": 10}}, "AGC Setpoint must be from -60 to 0"),
                                ({"device_settings": "on"}, "device's settings must be an object"),
                                ({"bias_tee": 1}, "the antenna power must be true or false")):
            with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, reason):
                soapy_source.changed(settings, changes, offered)
        # Until the device is known: any stage, socket or setting of a JSON type, which its opening fits.
        unknown = soapy_source.changed(settings, {"stage_gains_db": {"OTHER": 3}, "antenna": "X",
                                                  "device_settings": {"biastee": True}}, None)
        fitted = soapy_source.fitted(unknown, offered)
        self.assertEqual((dict(fitted.stage_gains), fitted.antenna, "biastee" in dict(fitted.device_settings)),
                         ({"IFGR": 40.0, "RFGR": 4.0}, "Antenna A", False))
        with self.assertRaisesRegex(ValueError, "the setting x must be true, false, a number or a short text"):
            soapy_source.changed(settings, {"device_settings": {"x": [1]}}, None)

    def test_a_held_stage_starts_at_its_lowest_unless_saved(self):
        with stand_in_soapysdr() as soapy:
            soapy.hackrf_one()
            offered = opened_capabilities(soapy, "hackrf")
        start = soapy_source.Settings(145e6, 2.4e6, None, 0.0)
        first = soapy_source.fitted(start, offered)
        self.assertEqual((dict(first.stage_gains), first.gain_db), ({"LNA": 16.0, "AMP": 0.0, "VGA": 16.0}, 32.0))
        saved = soapy_source.fitted(dataclasses.replace(start, stage_gains=(("AMP", 14.0), ("LNA", 8.0))), offered)
        self.assertEqual(dict(saved.stage_gains), {"LNA": 8.0, "AMP": 14.0, "VGA": 16.0})
        # Antenna power asked of a device without it: ignored, which the log says.
        with self.assertLogs("radiolack.soapy", logging.WARNING) as logs:
            none = soapy_source.fitted(dataclasses.replace(start, bias_tee=True), capabilities())
        self.assertFalse(none.bias_tee)
        self.assertIn("the antenna power asked is ignored: the test device has no antenna power", logs.output[0])


class DeviceControlsSourceTest(SourceCase):
    """A device's gain stages, antenna, settings and antenna power as the
    source asks for them, between reads, and reads them back."""

    async def opened(self, source: SoapySource):
        """The device that ``source`` opened, once its first settings apply."""
        await self.samples(source)
        return self.soapy.opened[-1]

    def power_on_at_opening(self, **changes):
        """An RTL-SDR whose antenna power is on as it opens, as a device
        left on by another program would be."""
        spec = self.soapy.rtl_sdr(**changes)
        spec.settings = tuple(dataclasses.replace(setting, value="true") if setting.key == "biastee" else setting
                              for setting in spec.settings)
        return spec

    async def test_a_hackrfs_amplifier_starts_off_and_the_overall_gain_never_turns_it_on(self):
        self.soapy.hackrf_one()
        source = self.source("driver=hackrf")
        device = await self.opened(source)
        self.assertEqual(device.gain_writes, [("AMP", 0.0)])
        state = source.describe()
        self.assertEqual((state["gain_db"], state["stage_gains_db"]), (32.0, {"LNA": 16.0, "AMP": 0.0, "VGA": 16.0}))
        self.assertEqual(state["gain_stages"][:2], [{"name": "LNA", "gains_db": [0.0, 8.0, 16.0, 24.0, 32.0, 40.0]},
                                                    {"name": "AMP", "gains_db": [0.0, 14.0]}])
        self.assertEqual((state["gains_db"][0], state["gains_db"][-1]), (0.0, 102.0))
        for overall in (30.0, 102.0, 0.0, 78.0):
            source.configure({"gain_db": overall})
            await eventually(lambda: sum(device.gain_db.values()) == overall)
            await eventually(lambda: source.describe()["gain_db"] == overall)
        self.assertEqual([gain for name, gain in device.gain_writes if name == "AMP"], [0.0])
        # A stage of its own: the overall gain read back.
        source.configure({"stage_gains_db": {"LNA": 8}})
        await eventually(lambda: device.gain_db["LNA"] == 8.0)
        await eventually(lambda: source.describe()["gain_db"] == 46.0)
        # The amplifier turned on by its own slider: the overall gain keeps it on.
        source.configure({"stage_gains_db": {"AMP": 14}})
        await eventually(lambda: source.describe()["gain_db"] == 60.0)
        self.assertEqual(source.describe()["gains_db"][-1], 116.0)
        source.configure({"gain_db": 116})
        await eventually(lambda: device.gain_db == {"LNA": 40.0, "AMP": 14.0, "VGA": 62.0})
        self.assertEqual([gain for name, gain in device.gain_writes if name == "AMP"], [0.0, 14.0])

    async def test_an_rspdx_r2s_stages_antenna_and_settings(self):
        self.soapy.rspdx_r2(refuses=("hdr_ctrl",))
        source = self.source("driver=sdrplay")
        device = await self.opened(source)
        state = source.describe()
        self.assertTrue(device.automatic_gain)
        self.assertEqual((state["gain_db"], state["stage_gains_db"], state["antenna"], state["antennas"]),
                         (None, {"IFGR": 40.0, "RFGR": 4.0}, "Antenna A", ["Antenna A", "Antenna B", "Antenna C"]))
        self.assertEqual((state["bias_tee"], state["device_refusal"]), (False, None))
        self.assertEqual([info["key"] for info in state["device_setting_info"]],
                         ["rfnotch_ctrl", "dabnotch_ctrl", "hdr_ctrl", "iqcorr_ctrl", "agc_setpoint"])
        source.configure({"antenna": "Antenna B", "stage_gains_db": {"RFGR": 6},
                          "device_settings": {"rfnotch_ctrl": True, "agc_setpoint": -40}})
        await eventually(lambda: (device.antenna, device.gain_db["RFGR"]) == ("Antenna B", 6.0))
        await eventually(lambda: device.setting_values["agc_setpoint"] == "-40")
        self.assertEqual(device.setting_values["rfnotch_ctrl"], "true")
        await eventually(lambda: source.describe()["device_settings"]["agc_setpoint"] == -40)
        # Refused: the value the device holds stays, and the state says why.
        with self.assertLogs("radiolack.soapy", logging.WARNING):
            source.configure({"device_settings": {"hdr_ctrl": True}})
            await eventually(lambda: source.describe()["device_refusal"] is not None)
        state = source.describe()
        self.assertEqual(state["device_refusal"], "the device refused HDR Mode: writeSetting(hdr_ctrl) refused")
        self.assertFalse(state["device_settings"]["hdr_ctrl"])
        self.assertEqual(device.setting_values["hdr_ctrl"], "false")
        source.configure({"device_settings": {"dabnotch_ctrl": True}})       # the next change clears it
        self.assertIsNone(source.describe()["device_refusal"])

    async def test_an_rtl_sdrs_direct_sampling_is_a_choice(self):
        self.soapy.rtl_sdr()
        source = self.source("driver=rtlsdr")
        device = await self.opened(source)
        state = source.describe()
        self.assertEqual((state["gain_stages"], state["antennas"], state["direct"], state["hf_inputs"]),
                         ([], [], False, []))
        self.assertEqual(state["device_setting_info"][0]["options"],
                         [{"value": "0", "name": "Off"}, {"value": "1", "name": "I-ADC"},
                          {"value": "2", "name": "Q-ADC"}])
        source.configure({"device_settings": {"direct_samp": "2"}})
        await eventually(lambda: device.setting_values["direct_samp"] == "2")
        self.assertEqual(source.describe()["device_settings"]["direct_samp"], "2")

    async def test_the_antenna_power_is_off_at_each_opening_and_before_each_release(self):
        self.power_on_at_opening()
        source = self.source("driver=rtlsdr", idle_s=0.2)
        device = await self.opened(source)
        self.assertEqual(device.events[:2], ["biastee=false", "setupStream"])
        self.assertFalse(source.describe()["bias_tee"])
        # Released once nobody reads: off first.
        await eventually(lambda: device.closed)
        self.assertEqual(device.events[-3:], ["biastee=false", "closeStream", "close"])
        # Opened again: off again, whatever the device says.
        device = await self.opened(source)
        self.assertEqual(device.events[:2], ["biastee=false", "setupStream"])
        self.assertFalse(any("biastee=true" in opened.events for opened in self.soapy.opened))

    async def test_the_antenna_power_is_off_after_an_unplugged_device_comes_back(self):
        spec = self.power_on_at_opening(unplugged_reads="error")
        source = self.source("driver=rtlsdr")
        source.configure({"bias_tee": False})
        device = await self.opened(source)
        spec.present = False
        await self.state(source, "waiting", "the stream failed: STREAM_ERROR")
        self.assertEqual(device.events[-3:], ["biastee=false", "closeStream", "close"])
        await self.state(source, "waiting", "no rtlsdr device found with its serial number")
        spec.present = True
        device = await self.opened(source)
        self.assertEqual(device.events[:2], ["biastee=false", "setupStream"])
        self.assertFalse(any("biastee=true" in opened.events for opened in self.soapy.opened))

    async def test_only_the_start_option_or_a_press_turns_it_on(self):
        # Asked before the device opens, as --bias-tee does: on once it opens, after the off.
        self.power_on_at_opening()
        source = self.source("driver=rtlsdr")
        source.configure({"bias_tee": True})
        device = await self.opened(source)
        self.assertEqual(device.events[:3], ["biastee=false", "setupStream", "biastee=true"])
        self.assertTrue(source.describe()["bias_tee"])
        # Turned off and on again by the interface's presses.
        source.configure({"bias_tee": False})
        await eventually(lambda: device.setting_values["biastee"] == "false")
        source.configure({"bias_tee": True})
        await eventually(lambda: device.setting_values["biastee"] == "true")
        await self.closed(source)
        self.assertEqual(device.events[-3:], ["biastee=false", "closeStream", "close"])

    async def test_turned_on_since_the_start_it_comes_back_on_at_the_next_opening(self):
        self.power_on_at_opening()
        source = self.source("driver=rtlsdr", idle_s=0.2)
        source.configure({"bias_tee": True})
        device = await self.opened(source)
        await eventually(lambda: device.closed)
        self.assertEqual(device.events[-3:], ["biastee=false", "closeStream", "close"])
        device = await self.opened(source)
        self.assertEqual(device.events[:3], ["biastee=false", "setupStream", "biastee=true"])

    async def test_several_antenna_power_settings_are_all_off_and_one_follows(self):
        spec, info = self.soapy.SettingSpec, self.soapy.ArgInfo
        self.device(settings=(spec("biastee", "true", "Bias Tee"),
                              spec("bias_tx", "1", "Antenna Bias", type=info.INT, range=(0.0, 5.0, 1.0), channel=True)))
        source = self.source()
        device = await self.opened(source)
        self.assertEqual(device.events[:3], ["bias_tx=0", "biastee=false", "setupStream"])
        source.configure({"bias_tee": True})
        await eventually(lambda: device.setting_values["bias_tx"] == "1")
        self.assertEqual(device.setting_values["biastee"], "false")
        await self.closed(source)
        self.assertEqual(device.events[-4:], ["bias_tx=0", "biastee=false", "closeStream", "close"])

    async def test_an_off_refused_is_logged_and_the_others_still_go_off(self):
        spec = self.soapy.SettingSpec
        self.device(settings=(spec("biastee", "true", "Bias Tee"), spec("bias_b", "true", "Second bias")),
                    refuses=("biastee",))
        source = self.source()
        with self.assertLogs("radiolack.soapy", logging.WARNING) as logs:
            device = await self.opened(source)
        self.assertEqual(device.setting_values, {"biastee": "true", "bias_b": "false"})
        self.assertIn("the antenna power biastee could not be turned off: writeSetting(biastee) refused",
                      logs.output[0])

    async def test_a_device_without_antenna_power_ignores_it(self):
        self.device()
        source = self.source()
        source.configure({"bias_tee": True})
        with self.assertLogs("radiolack.soapy", logging.WARNING) as logs:
            await self.opened(source)
        self.assertTrue(any("the antenna power asked is ignored" in line for line in logs.output))
        state = source.describe()
        self.assertEqual((state["bias_tee"], "bias_tee" in state["settable"]), (None, False))


class StandInTest(unittest.TestCase):
    def test_the_stand_in_is_what_the_tests_import(self):
        with stand_in_soapysdr() as soapy:
            self.assertEqual(Path(soapy.__file__).parent, STAND_IN_SOAPYSDR)


if __name__ == "__main__":
    unittest.main()
