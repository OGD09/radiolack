"""Tests for the tool that measures a Python thread reading a SoapySDR
receiver (tools/measure_reader.py), against the stand-in SoapySDR module
(tests/soapysdr/SoapySDR.py), which they check too: never a device, and the
figures left alone. What they check is what the stand-in scripts."""

import importlib.util
import io
import re
import sys
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

from tests.fakes import STAND_IN_SOAPYSDR, stand_in_soapysdr

TOOLS = Path(__file__).resolve().parents[1] / "tools"
RATE_HZ = 240_000.0


def load(name):
    spec = importlib.util.spec_from_file_location(name, TOOLS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module     # its dataclasses look their module up by this name
    spec.loader.exec_module(module)
    return module


reader = load("measure_reader")


def read(soapy, *options, seconds="1"):
    out, err = io.StringIO(), io.StringIO()
    status = reader.main(["driver=test", "--rate", str(RATE_HZ / 1e6), "--seconds", seconds, *options], out, err)
    return status, out.getvalue(), err.getvalue()


def row(text, name):
    for line in text.splitlines():
        if line.startswith(f"| {name} |"):
            return line.split("|")[2].strip()
    raise AssertionError(f"no row {name!r} in {text}")


class ReaderTest(unittest.TestCase):
    def test_a_reader_that_loses_nothing_keeps_up_and_closes_the_device(self):
        with stand_in_soapysdr() as soapy:
            soapy.add("test", rates_hz=(RATE_HZ,), mtu=4096)
            status, text, err = read(soapy, "--without-engine")
            device = soapy.opened[0]
            self.assertTrue(device.closed)
            self.assertTrue(all(stream.closed and not stream.active for stream in device.streams))
        self.assertEqual(status, reader.EXIT_KEPT_UP, err)
        received = int(row(text, "Samples received"))
        self.assertGreater(received, 0)
        self.assertEqual(row(text, "Samples the timestamps span"), f"{received} (0 not received)")
        self.assertEqual(row(text, "Overflows the device reported"), "0")
        self.assertEqual(row(text, "Gaps in the timestamps"), "0 (0 samples)")
        self.assertIn("native format CS8", text)
        self.assertIn("The reader kept up: no sample lost.", text)

    def test_overflows_gaps_and_errors_are_counted_and_lose(self):
        with stand_in_soapysdr() as soapy:
            soapy.add("test", rates_hz=(RATE_HZ,), mtu=4096, drop=1000,
                      events={2: "overflow", 4: "timeout", 5: "error", 9: "overflow"})
            status, text, err = read(soapy)
        self.assertEqual(status, reader.EXIT_LOST, err)
        self.assertEqual(row(text, "Overflows the device reported"), "2")
        self.assertEqual(row(text, "Gaps in the timestamps"), "2 (2000 samples)")
        received = int(row(text, "Samples received"))
        self.assertEqual(row(text, "Samples the timestamps span"), f"{received + 2000} (2000 not received)")
        self.assertRegex(row(text, "Reads"), r"^\d+, [1-9]\d* timed out$")
        self.assertEqual(row(text, "Stream errors"), "STREAM_ERROR 1")
        self.assertIn("by the rule, the reader moves to a process of its own", text)

    def test_without_timestamps_an_overflow_still_loses(self):
        with stand_in_soapysdr() as soapy:
            soapy.add("test", rates_hz=(RATE_HZ,), mtu=4096, timestamps=False, events={3: "overflow"})
            status, text, _ = read(soapy, "--without-engine")
        self.assertEqual(status, reader.EXIT_LOST)
        self.assertEqual(row(text, "Samples the timestamps span"), "no timestamps")
        self.assertRegex(row(text, "Samples the wall clock expects since the first came"), r"^\d+ \(-?\d+\.\d\d% short\)$")
        self.assertEqual(row(text, "Overflows the device reported"), "1")

    def test_a_stream_that_keeps_failing_ends_the_measure(self):
        with stand_in_soapysdr() as soapy:
            soapy.add("test", rates_hz=(RATE_HZ,), events={n: "error" for n in range(1, 40)})
            status, text, _ = read(soapy, "--without-engine", seconds="30")
        self.assertEqual(status, reader.EXIT_LOST)
        self.assertIn("The measure stopped early: the stream failed 10 times in a row: STREAM_ERROR.", text)

    def test_the_engine_processes_what_the_reader_brings(self):
        with stand_in_soapysdr() as soapy:
            soapy.add("test", rates_hz=(RATE_HZ,), native="CS16", formats=("CS16", "CF32"))
            status, text, err = read(soapy, seconds="1.5")
        self.assertEqual(status, reader.EXIT_KEPT_UP, err)
        self.assertIn("native format CS16", text)
        self.assertIn("processed as the engine does", text)
        blocks = int(re.match(r"(\d+),", row(text, "The engine's blocks"))[1])
        self.assertGreater(blocks, 0)

    def test_the_native_format_without_the_bindings_call(self):
        with stand_in_soapysdr() as soapy:
            for native, formats, expected in (("CS16", ("CS16", "CF32"), "CS16"), ("CF32", ("CF32", "CS16"), "CF32"),
                                              ("CS8", ("CU8", "CS8"), "CS8"), ("CU8", ("CU8",), "CF32")):
                with self.subTest(formats=formats):
                    soapy.reset()
                    soapy.add("test", native=native, formats=formats)
                    self.assertEqual(reader.native_format(soapy, soapy.Device("driver=test")), expected)

    def test_samples_read_convert_to_the_stand_ins(self):
        with stand_in_soapysdr() as soapy:
            for name in ("CS8", "CS16", "CF32"):
                with self.subTest(format=name):
                    soapy.reset()
                    soapy.add("test", native=name, rates_hz=(RATE_HZ,))
                    device = soapy.Device({"driver": "test"})
                    stream = device.setupStream(soapy.SOAPY_SDR_RX, name, [0])
                    device.activateStream(stream)
                    kind = reader.FORMATS[name]
                    array = np.empty(2 * 4096, kind.component)
                    result = device.readStream(stream, [array], 4096, 0, 1_000_000)
                    self.assertGreater(result.ret, 0)
                    samples = reader.to_complex(memoryview(array)[:2 * result.ret].cast("B"), kind)
                    np.testing.assert_allclose(samples, soapy.samples(0, result.ret), atol=1 / kind.full_scale)

    def test_without_the_module_or_the_device_it_says_so(self):
        with mock.patch.dict(sys.modules, {"SoapySDR": None}):
            status, _, err = read(None)
        self.assertEqual(status, reader.EXIT_UNABLE)
        self.assertIn("python3-soapysdr", err)
        with stand_in_soapysdr() as soapy:
            soapy.add("other")
            status, _, err = read(soapy)
            self.assertEqual(status, reader.EXIT_UNABLE)
            self.assertIn("cannot open the device 'driver=test'", err)
            soapy.add("test", rates_hz=(1e6,))
            status, _, err = read(soapy)        # a rate the device has not
            self.assertEqual(status, reader.EXIT_UNABLE)
            self.assertIn("cannot set the device up", err)
            self.assertTrue(soapy.opened[0].closed)

    def test_the_tool_names_no_transmit_direction(self):
        source = (TOOLS / "measure_reader.py").read_text(encoding="utf-8")
        for name in ("SOAPY_SDR_TX", "writeStream", "readStreamStatus"):
            self.assertNotIn(name, source)

    def test_options_are_checked(self):
        for argv in ([], ["driver=x", "--rate", "0"], ["driver=x", "--seconds", "-1"], ["driver=x", "--format", "CU8"]):
            with self.subTest(argv=argv), self.assertRaises(SystemExit):
                with mock.patch("sys.stderr", io.StringIO()):
                    reader.build_parser().parse_args(argv)


class StandInTest(unittest.TestCase):
    """The stand-in that pull request 2's source will be tested against."""

    def test_it_replaces_the_real_module_and_puts_it_back(self):
        before = sys.modules.get("SoapySDR")
        with stand_in_soapysdr() as soapy:
            import SoapySDR
            self.assertIs(SoapySDR, soapy)
            self.assertEqual(Path(soapy.__file__).parent, STAND_IN_SOAPYSDR)
        self.assertIs(sys.modules.get("SoapySDR"), before)
        self.assertNotIn(str(STAND_IN_SOAPYSDR), sys.path)

    def test_a_search_finds_the_devices_of_its_driver_and_is_kept(self):
        with stand_in_soapysdr() as soapy:
            soapy.add("hackrf", serial="a1")
            soapy.add("sdrplay", serial="b2", label="RSPdx-R2")
            self.assertEqual(soapy.Device.enumerate({"driver": "sdrplay"}),
                             [{"driver": "sdrplay", "serial": "b2", "label": "RSPdx-R2"}])
            self.assertEqual(soapy.Device.enumerate("driver=airspy"), [])
            self.assertEqual(soapy.searches, [{"driver": "sdrplay"}, {"driver": "airspy"}])
            self.assertEqual(soapy.Device("driver=hackrf,serial=a1").getDriverKey(), "hackrf")
            soapy.add("lime", fail_open=True)
            with self.assertRaises(RuntimeError):
                soapy.Device({"driver": "lime"})

    def test_every_transmit_request_fails_the_test(self):
        for ask in (lambda soapy, device: device.setupStream(soapy.SOAPY_SDR_TX, "CS8", [0]),
                    lambda soapy, device: device.setSampleRate(soapy.SOAPY_SDR_TX, 0, 2.4e6),
                    lambda soapy, device: device.setFrequency(soapy.SOAPY_SDR_TX, 0, 1e8),
                    lambda soapy, device: device.getStreamFormats(soapy.SOAPY_SDR_TX, 0),
                    lambda soapy, device: device.writeStream(None, [], 0)):
            with self.subTest(ask=ask), self.assertRaisesRegex(AssertionError, "transmit direction"):
                with stand_in_soapysdr() as soapy:
                    soapy.add("hackrf")
                    try:
                        ask(soapy, soapy.Device("driver=hackrf"))
                    except soapy.TransmitRequested:
                        pass        # caught by the code under test: the test still fails

    def test_a_timeout_comes_when_no_sample_does(self):
        with stand_in_soapysdr() as soapy:
            soapy.add("test", rates_hz=(1.0,))     # one sample a second
            device = soapy.Device("driver=test")
            stream = device.setupStream(soapy.SOAPY_SDR_RX, "CS8", [0])
            array = np.empty(64, np.int8)
            self.assertEqual(device.readStream(stream, [array], 32).ret, soapy.SOAPY_SDR_STREAM_ERROR)  # not active
            device.activateStream(stream)
            self.assertEqual(device.readStream(stream, [array], 32, 0, 1000).ret, soapy.SOAPY_SDR_TIMEOUT)


if __name__ == "__main__":
    unittest.main()
