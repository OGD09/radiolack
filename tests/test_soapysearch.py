"""Tests of the search for SoapySDR devices (soapysearch.py) and of the
engine's devices: the search, the choice of a device found and the device
arguments, against the stand-in SoapySDR module only, never a receiver.

Every search must name exactly one listed driver, never none and never
rtlsdr: the stand-in keeps every search's arguments (``searches``)."""

import asyncio
import json
import re
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from radiolack import soapysearch
from radiolack.engine import Engine, Subscription
from radiolack.server import _apply_command
from radiolack.soapy import MISSING, SoapySource
from radiolack.soapysearch import LISTED_DRIVERS, NEVER_SEARCHED, Device, DeviceSearch, device, search
from radiolack.sources import Sources
from radiolack.store import SETTINGS_FILE, Store
from tests.fakes import FakeSource, stand_in_soapysdr
from tests.test_engine import eventually

REFUSED = "only Radiolack's interface on the computer may change this"
SEARCH_FILE = Path(soapysearch.__file__)
WAIT_S = 10.0


def listed_searches(searches: list[dict[str, str]]) -> list[str]:
    """The drivers that the searches named, each search checked to name one
    listed driver alone."""
    drivers = []
    for wanted in searches:
        if set(wanted) != {"driver"} or wanted["driver"] not in LISTED_DRIVERS:
            raise AssertionError(f"a search for anything but one listed driver alone: {wanted}")
        drivers.append(wanted["driver"])
    return drivers


def without_source(searches: list[dict[str, str]], driver: str) -> list[dict[str, str]]:
    """``searches`` without those of the SoapySDR source in use, of
    ``driver``, which opens its device: with its driver, and once found its
    serial number."""
    return [wanted for wanted in searches if wanted.get("driver") != driver]


class ListedDriversTest(unittest.TestCase):
    def test_the_listed_drivers_are_local_usb_ones_without_rtlsdr(self):
        self.assertEqual(LISTED_DRIVERS, ("hackrf", "airspy", "airspyhf", "lime", "bladerf"))
        self.assertEqual(NEVER_SEARCHED, ("rtlsdr",))
        for drivers in (("rtlsdr",), ("hackrf", "")):
            with self.subTest(drivers=drivers), self.assertRaises(ValueError):
                DeviceSearch(drivers)

    def test_the_search_opens_nothing_and_names_no_transmit_direction(self):
        text = SEARCH_FILE.read_text(encoding="utf-8")
        for name in ("SOAPY_SDR_TX", "writeStream", "TX", "setupStream", "make(", "soapy.Device("):
            with self.subTest(name=name):
                self.assertNotIn(name, text)
        # The module's devices: searched, never made.
        self.assertEqual(set(re.findall(r"soapy\.Device\.(\w+)", text)), {"enumerate"})


class SearchTest(unittest.TestCase):
    def setUp(self):
        self.soapy = self.enterContext(stand_in_soapysdr())

    def found(self, drivers=LISTED_DRIVERS):
        return search(lambda: (self.soapy, ""), drivers)

    def test_each_listed_driver_alone_in_turn(self):
        self.soapy.add("hackrf", serial="0001", label="HackRF One #0 0001")
        self.soapy.add("airspy", serial="0002")
        self.soapy.add("rtlsdr", serial="0003")     # never searched
        found = self.found()
        self.assertEqual(listed_searches(self.soapy.searches), list(LISTED_DRIVERS))
        self.assertEqual(found.devices, (Device("hackrf", "0001", "HackRF One #0 0001"),
                                         Device("airspy", "0002", "airspy 0002")))
        self.assertEqual((found.failures, found.problem), ((), ""))

    def test_a_search_that_fails_leaves_the_others(self):
        self.soapy.add("lime", serial="0004")
        self.soapy.search_failures["airspy"] = "libairspy failed"
        found = self.found()
        self.assertEqual(listed_searches(self.soapy.searches), list(LISTED_DRIVERS))
        self.assertEqual(found.devices, (Device("lime", "0004", "lime 0004"),))
        self.assertEqual(found.failures, (("airspy", "libairspy failed"),))

    def test_without_the_module_nothing_is_searched(self):
        found = search(lambda: (None, MISSING), LISTED_DRIVERS)
        self.assertEqual((found.devices, found.problem), ((), MISSING))
        self.assertEqual(self.soapy.searches, [])

    def test_rtlsdr_and_no_driver_are_never_searched(self):
        for drivers in (("rtlsdr",), ("",)):
            with self.subTest(drivers=drivers), self.assertRaises(ValueError):
                self.found(drivers)
        self.assertEqual(self.soapy.searches, [])

    def test_what_is_kept_of_a_listing(self):
        self.assertEqual(device("hackrf", "0001", "HackRF\tOne\n#0  0001"), Device("hackrf", "0001", "HackRF One #0 0001"))
        self.assertEqual(device("hackrf", "a,b=c", ""), Device("hackrf", "", "hackrf"))     # arguments cannot carry it
        self.assertEqual(device("hackrf", "9" * 65, "x"), Device("hackrf", "", "x"))
        self.assertEqual(device("hackrf", None, None), Device("hackrf", "", "hackrf"))
        self.assertEqual(len(device("hackrf", "1", "x" * 300).label), soapysearch.MAX_LABEL)
        self.assertEqual(Device("hackrf", "0001", "x").args, "driver=hackrf,serial=0001")
        self.assertEqual(Device("hackrf", "", "x").args, "driver=hackrf")
        # The same device twice is listed once; no bus position or other key is kept.
        self.soapy.add("bladerf", serial="0005", label="bladeRF 0005")
        self.soapy.add("bladerf", serial="0005", label="bladeRF 0005")
        found = self.found(("bladerf",))
        self.assertEqual(found.devices, (Device("bladerf", "0005", "bladeRF 0005"),))
        self.assertEqual(found.devices[0].describe(False),
                         {"driver": "bladerf", "serial": "0005", "label": "bladeRF 0005", "in_use": False})


class EngineCase(unittest.IsolatedAsyncioTestCase):
    """An engine that opens every kind of source, the stand-in SoapySDR
    module in place of the real one."""

    async def asyncSetUp(self):
        self.soapy = self.enterContext(stand_in_soapysdr())
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory())) / "radiolack"

    def engine(self, **options) -> Engine:
        engine = Engine(sources=Sources(), store=Store(self.directory), **options)
        task = asyncio.ensure_future(engine.run())

        async def stop():
            engine.close()
            await asyncio.wait_for(task, WAIT_S)
            await asyncio.wait_for(engine.wait_closed(), 15)

        self.addAsyncCleanup(stop)
        self.stop = stop
        return engine

    async def devices(self, subscription: Subscription, until) -> dict:
        """The next devices state that ``subscription`` receives and that
        satisfies ``until``."""
        async def wait() -> dict:
            while True:
                message = await subscription.get()
                if message is None:
                    raise AssertionError("the engine stopped")
                if isinstance(message, dict) and message["type"] == "devices" and until(message):
                    return message

        return await asyncio.wait_for(wait(), WAIT_S)

    async def searched(self, engine: Engine, subscription: Subscription) -> dict:
        engine.configure_devices({"search": True})
        return await self.devices(subscription, lambda state: state["search"] not in ("never", "searching"))


class EngineTest(EngineCase):
    async def test_no_search_at_start_nor_for_a_remote_client(self):
        self.soapy.add("hackrf")
        engine = self.engine()
        local, remote = engine.subscribe(), engine.subscribe(device="phone")
        for subscription in (local, remote):
            state = await self.devices(subscription, lambda state: True)
            self.assertEqual(state, {"type": "devices", "search": "never", "detail": "", "drivers": list(LISTED_DRIVERS),
                                     "devices": [], "chosen": False, "args": None})
        command = json.dumps({"type": "devices", "search": True})
        self.assertEqual(_apply_command(engine, remote, None, command), ("devices", REFUSED))
        await asyncio.sleep(0)
        self.assertEqual(self.soapy.searches, [])
        self.assertEqual(engine.devices_state()["search"], "never")

    async def test_a_search_lists_what_it_found_and_runs_one_at_a_time(self):
        hackrf = self.soapy.add("hackrf", serial="0001", label="HackRF One #0 0001")
        engine = self.engine()
        subscription = engine.subscribe()
        self.soapy.search_gate = gate = threading.Event()
        engine.configure_devices({"search": True})
        await self.devices(subscription, lambda state: state["search"] == "searching")
        engine.configure_devices({"search": True})      # one at a time: nothing more
        with self.assertRaisesRegex(ValueError, "a search for SoapySDR devices is running: choose once it ends"):
            engine.configure_devices({"args": "driver=hackrf"})
        with self.assertRaisesRegex(ValueError, "a search for SoapySDR devices is running"):
            engine.configure({"kind": "soapy"})
        gate.set()
        state = await self.devices(subscription, lambda state: state["search"] == "done")
        self.assertEqual(state["devices"], [{"driver": "hackrf", "serial": "0001", "label": "HackRF One #0 0001",
                                             "in_use": False}])
        self.assertEqual(listed_searches(self.soapy.searches), list(LISTED_DRIVERS))
        # A device that goes and one that comes, between two searches.
        hackrf.present = False
        self.soapy.add("airspy", serial="0002")
        state = await self.searched(engine, subscription)
        self.assertEqual([entry["serial"] for entry in state["devices"]], ["0002"])
        self.assertEqual(listed_searches(self.soapy.searches), list(LISTED_DRIVERS) * 2)
        self.assertEqual(engine.source.kind, "synthetic")      # nothing opened

    async def test_a_search_that_fails_and_one_without_the_module(self):
        self.soapy.add("lime", serial="0004")
        self.soapy.search_failures["hackrf"] = "libhackrf failed"
        engine = self.engine()
        subscription = engine.subscribe()
        state = await self.searched(engine, subscription)
        self.assertEqual((state["search"], state["detail"]), ("failed", "the hackrf search failed: libhackrf failed"))
        self.assertEqual([entry["driver"] for entry in state["devices"]], ["lime"])
        with mock.patch.object(engine._device_search, "_load", lambda: (None, MISSING)):
            state = await self.searched(engine, subscription)
        self.assertEqual((state["search"], state["detail"], state["devices"]), ("unavailable", MISSING, []))
        self.assertEqual(len(self.soapy.searches), len(LISTED_DRIVERS))     # none without the module

    async def test_a_search_that_stops_unexpectedly_leaves_search_again_working(self):
        engine = self.engine()
        subscription = engine.subscribe()
        with mock.patch.object(engine._device_search, "run", side_effect=RuntimeError("unexpected")), \
                self.assertLogs("radiolack.engine", "ERROR"):
            engine.configure_devices({"search": True})
            self.assertEqual(engine.devices_state()["search"], "searching")
            await asyncio.wait_for(engine._search_task, WAIT_S)
        self.assertEqual(engine.devices_state()["search"], "never")
        self.assertEqual(self.soapy.searches, [])
        self.assertEqual((await self.searched(engine, subscription))["search"], "done")

    async def test_choosing_a_device_found_opens_it_and_saves_it_by_driver_and_serial(self):
        self.soapy.add("hackrf", serial="0001", label="HackRF One #0 0001", formats=("CS8",))
        self.soapy.add("hackrf", serial="0002", label="HackRF One #1 0002", formats=("CS8",))
        engine = self.engine()
        subscription = engine.subscribe()
        await self.searched(engine, subscription)
        for refused, reason in (({"driver": "hackrf", "serial": "0009"}, "not among those found"),
                                ({"driver": "hackrf"}, "choose names a device found"),
                                ("hackrf", "choose names a device found")):
            with self.subTest(choose=refused), self.assertRaisesRegex(ValueError, reason):
                engine.configure_devices({"choose": refused})
        self.assertEqual(engine.source.kind, "synthetic")
        engine.configure_devices({"choose": {"driver": "hackrf", "serial": "0002"}})
        self.assertIsInstance(engine.source, SoapySource)
        self.assertEqual(engine.source.reopen, {"kind": "soapy", "args": "driver=hackrf,serial=0002"})
        state = await self.devices(subscription, lambda state: state["devices"][0]["in_use"]
                                   and state["devices"][0]["label"] == "HackRF One #1 0002")
        self.assertEqual([(entry["serial"], entry["in_use"]) for entry in state["devices"]],
                         [("0002", True), ("0001", False)])
        self.assertEqual((state["chosen"], state["args"]), (True, "driver=hackrf,serial=0002"))
        self.assertIsNone(engine.devices_state(remote=True)["args"])
        await eventually(lambda: engine.state()["status"] == "streaming")
        engine.configure({"ppm": 3.5})
        # While it streams, its driver is not searched again: its devices stay as found.
        before = len(self.soapy.searches)
        state = await self.searched(engine, subscription)
        drivers = listed_searches(without_source(self.soapy.searches[before:], "hackrf"))
        self.assertEqual(drivers, [driver for driver in LISTED_DRIVERS if driver != "hackrf"])
        self.assertEqual([entry["serial"] for entry in state["devices"]], ["0002", "0001"])
        await self.stop()
        saved = json.loads((self.directory / SETTINGS_FILE).read_text(encoding="utf-8"))
        self.assertEqual(saved["source"], {"kind": "soapy", "args": "driver=hackrf,serial=0002"})
        self.assertEqual(saved["receivers"]["soapy hackrf 0002"]["source"]["ppm"], 3.5)

    async def test_device_arguments_open_any_device_and_are_checked(self):
        self.soapy.add("rtlsdr", serial="00000001", label="Generic RTL2832U OEM :: 00000001", formats=("CS8",))
        engine = self.engine()
        subscription = engine.subscribe()
        for text, reason in (("", "no device arguments given"), ("serial=0001", "must name a driver"),
                             ("driver", "KEY=VALUE"), ("driver=a,driver=b", "given twice"),
                             ("driver=" + "a" * 300, "longer than"), (7, "must be text")):
            with self.subTest(args=text), self.assertRaisesRegex(ValueError, reason):
                engine.configure_devices({"args": text})
        for message, reason in (({}, "asks for one thing"), ({"search": True, "args": "driver=rtlsdr"}, "one thing"),
                                ({"search": 1}, "search must be true"), ({"open": "x"}, "unknown devices field")):
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, reason):
                engine.configure_devices(message)
        self.assertEqual(engine.source.kind, "synthetic")
        # driver=rtlsdr through the arguments: the device in use is listed, though rtlsdr is never searched.
        engine.configure_devices({"args": "driver=rtlsdr"})
        await eventually(lambda: engine.state()["status"] == "streaming")
        state = await self.devices(subscription, lambda state: state["devices"]
                                   and state["devices"][0]["serial"] == "00000001")
        self.assertEqual(state["devices"], [{"driver": "rtlsdr", "serial": "00000001",
                                             "label": "Generic RTL2832U OEM :: 00000001", "in_use": True}])
        self.assertEqual(state["args"], "driver=rtlsdr,serial=00000001")
        self.assertEqual(without_source(self.soapy.searches, "rtlsdr"), [])     # the source's own, to open it
        state = await self.searched(engine, subscription)
        self.assertEqual(listed_searches(without_source(self.soapy.searches, "rtlsdr")), list(LISTED_DRIVERS))
        self.assertEqual([entry["driver"] for entry in state["devices"]], ["rtlsdr"])
        # The same device again changes nothing; the simulated band back, then the device used last.
        source = engine.source
        engine.configure_devices({"args": "driver=rtlsdr,serial=00000001"})
        self.assertIs(engine.source, source)
        engine.configure({"kind": "synthetic"})
        state = await self.devices(subscription, lambda state: not state["devices"])
        self.assertEqual((state["chosen"], state["args"]), (True, None))
        engine.configure({"kind": "soapy"})
        self.assertEqual(engine.source.reopen, {"kind": "soapy", "args": "driver=rtlsdr,serial=00000001"})

    async def test_a_remote_client_chooses_among_the_devices_found_only(self):
        self.soapy.add("airspy", serial="0002", formats=("CS16", "CF32"), native="CS16")
        engine = self.engine()
        local, remote = engine.subscribe(), engine.subscribe(device="phone")
        await self.searched(engine, local)
        self.assertEqual(listed_searches(self.soapy.searches), list(LISTED_DRIVERS))
        for message in ({"type": "devices", "search": True}, {"type": "devices", "args": "driver=airspy"}):
            with self.subTest(message=message):
                self.assertEqual(_apply_command(engine, remote, None, json.dumps(message)), ("devices", REFUSED))
        await asyncio.sleep(0)
        self.assertEqual((engine.source.kind, engine.devices_state()["search"]), ("synthetic", "done"))
        self.assertEqual(len(self.soapy.searches), len(LISTED_DRIVERS))     # no search more
        choose = {"type": "devices", "choose": {"driver": "airspy", "serial": "0002"}}
        self.assertEqual(_apply_command(engine, remote, None, json.dumps(choose)), ("devices", None))
        self.assertEqual(engine.source.reopen, {"kind": "soapy", "args": "driver=airspy,serial=0002"})
        state = await self.devices(remote, lambda state: state["devices"] and state["devices"][0]["in_use"])
        self.assertIsNone(state["args"])


class NoSoapyTest(unittest.IsolatedAsyncioTestCase):
    async def test_an_engine_without_soapysdr_sources_has_no_devices(self):
        engine = Engine(FakeSource())
        self.assertNotIn("devices", engine._states)
        with self.assertRaisesRegex(ValueError, "this Radiolack does not open SoapySDR devices"):
            engine.configure_devices({"search": True})
        engine.close()
        await engine.wait_closed()


if __name__ == "__main__":
    unittest.main()
