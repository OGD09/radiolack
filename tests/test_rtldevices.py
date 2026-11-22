"""Tests for the list of the RTL-SDR receivers plugged in, read from a fake
sysfs tree, and the engine's rtl_devices message. Nothing here opens a
receiver: no rtl_tcp starts, as no client listens."""

import asyncio
import shutil
import tempfile
import unittest
from pathlib import Path

from radiolack.engine import Engine
from radiolack.rtldevices import KNOWN_DEVICES, MAX_DEVICES, RtlDevice, list_devices, reads_as_index
from radiolack.rtltcp import RtlSdrSource
from radiolack.sources import Sources
from radiolack.store import SETTINGS_FILE, Store
from tests.fakes import FAKE_RTL_TCP, plug_usb_device
from tests.test_engine import ready_messages

NESDR = {"manufacturer": "Nooelec", "product": "NESDR SMArt v5", "serial": "59196661"}
BLOG = {"manufacturer": "Realtek", "product": "RTL2838UHIDIR", "serial": "RTLSDR1"}


class ListTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def test_lists_the_known_ids_alone_in_the_bus_order(self):
        plug_usb_device(self.root, "3-1.3", **NESDR)
        plug_usb_device(self.root, "1-1", **BLOG)
        plug_usb_device(self.root, "3-1.4", "1ea7", "0066", manufacturer=None, product="Keyboard", serial=None)
        plug_usb_device(self.root, "usb1", "1d6b", "0002", product="xHCI Host Controller", serial="0000:00:14.0")
        plug_usb_device(self.root, "3-1.10", "1b80", "d3a4", manufacturer=None, product=None, serial="7")
        (self.root / "3-0:1.0").mkdir()     # a hub's interface, as /sys/bus/usb/devices lists them
        self.assertEqual(list_devices(self.root), [
            RtlDevice("RTLSDR1", "Realtek", "RTL2838UHIDIR", ()),
            RtlDevice("59196661", "Nooelec", "NESDR SMArt v5", ()),
            # No product string: librtlsdr's name for its IDs.
            RtlDevice("7", "", "Twintech UT-40", ()),
        ])

    def test_the_drivers_tell_who_holds_a_receiver(self):
        plug_usb_device(self.root, "1-1", serial="A", drivers=("usbfs", None))
        plug_usb_device(self.root, "1-2", serial="B", drivers=("dvb_usb_rtl28xxu", "dvb_usb_rtl28xxu"))
        plug_usb_device(self.root, "1-3", serial="C", drivers=(None, None))
        self.assertEqual([device.drivers for device in list_devices(self.root)],
                         [("usbfs",), ("dvb_usb_rtl28xxu",), ()])

    def test_marks_the_receivers_that_share_a_serial_number(self):
        plug_usb_device(self.root, "1-1", serial="00000001")
        plug_usb_device(self.root, "1-2", serial="00000001")
        plug_usb_device(self.root, "1-3", serial="00000002")
        plug_usb_device(self.root, "1-4", serial=None)
        plug_usb_device(self.root, "1-5", serial=None)
        self.assertEqual([(device.serial, device.shared, device.choosable) for device in list_devices(self.root)],
                         [("00000001", True, False), ("00000001", True, False), ("00000002", False, False),
                          ("", False, False), ("", False, False)])

    def test_marks_the_serial_numbers_that_rtl_tcp_reads_as_an_index(self):
        # rtl_tcp's -d reads "00000001" as index 1 with two receivers, as strtol does.
        plug_usb_device(self.root, "1-1", serial="00000001")
        plug_usb_device(self.root, "1-2", serial="59196661")
        self.assertEqual([(device.reads_as_index, device.choosable) for device in list_devices(self.root)],
                         [(True, False), (False, True)])
        shutil.rmtree(self.root / "1-2")
        self.assertEqual([(device.reads_as_index, device.choosable) for device in list_devices(self.root)],
                         [(False, True)])

    def test_reads_as_index_as_strtol_does(self):
        for text, count, expected in (("00000001", 2, True), ("00000001", 1, False), ("59196661", 2, False),
                                      ("RTLSDR1", 2, False), ("0", 1, True), ("0x1", 2, True), ("010", 9, True),
                                      ("010", 8, False), ("08", 9, False), ("0x", 2, False), (" 1", 2, True),
                                      ("1 ", 2, False), ("-0", 2, True), ("-1", 2, False), ("+1", 2, True),
                                      ("", 1, True), ("", 0, False), ("1_0", 20, False), ("\u0663", 5, False)):
            with self.subTest(text=text, count=count):
                self.assertIs(reads_as_index(text, count), expected)

    def test_strings_are_printable_and_short(self):
        plug_usb_device(self.root, "1-1", manufacturer="Odd\x07 maker", product="P" * 300, serial="S\x00N\n")
        device, = list_devices(self.root)
        self.assertEqual((device.manufacturer, device.name, device.serial), ("Odd maker", "P" * 64, "SN"))

    def test_leaves_out_what_cannot_be_read(self):
        plug_usb_device(self.root, "1-1", vendor="0bda", product_id="xyz1")
        plug_usb_device(self.root, "1-2", vendor="bda", product_id="2838")
        (self.root / "1-3").mkdir()
        (self.root / "1-4").write_text("not a folder")
        self.assertEqual(list_devices(self.root), [])

    def test_lists_some_at_most(self):
        for number in range(MAX_DEVICES + 3):
            plug_usb_device(self.root, f"1-{number + 1}", serial=str(number))
        self.assertEqual(len(list_devices(self.root)), MAX_DEVICES)

    def test_none_without_the_folder(self):
        self.assertIsNone(list_devices(self.root / "missing"))

    def test_the_table_holds_librtlsdrs_ids(self):
        self.assertEqual(len(KNOWN_DEVICES), 42)
        self.assertEqual(KNOWN_DEVICES[0x0BDA, 0x2838], "Generic RTL2832U OEM")


class EngineListTest(unittest.IsolatedAsyncioTestCase):
    """The engine's list, with Sources reading a fake tree and a fake rtl_tcp program."""

    async def asyncSetUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.store = Store(self.root / "config")

    def engine(self, **settings) -> Engine:
        if settings:
            self.store.save(SETTINGS_FILE, settings)
        engine = Engine(sources=Sources(rtl_tcp_program=FAKE_RTL_TCP, usb_devices=self.root / "usb"),
                        store=self.store)

        async def stop() -> None:
            engine.close()
            await asyncio.wait_for(engine.wait_closed(), timeout=5)
        self.addAsyncCleanup(stop)
        return engine

    @staticmethod
    def listed(engine: Engine, subscription, **changes) -> dict:
        ready_messages(subscription)
        engine.radios[0].configure_rtl_devices(changes or {"list": True}, subscription)
        reply, = (message for message in ready_messages(subscription) if message["type"] == "rtl_devices")
        return reply

    async def test_lists_the_receivers_plugged_in_each_time_it_is_asked(self):
        engine = self.engine()
        subscription = engine.subscribe()
        self.assertTrue(engine.lists_rtl_devices)
        # No state of its own: nothing is read until a client asks.
        self.assertNotIn("rtl_devices", [message["type"] for message in ready_messages(subscription)])
        self.assertEqual(self.listed(engine, subscription), {"type": "rtl_devices", "listing": "unavailable",
                                                             "devices": []})
        plug_usb_device(self.root / "usb", "1-1", **BLOG, drivers=("usbfs",))
        plug_usb_device(self.root / "usb", "1-2", **NESDR, drivers=("dvb_usb_rtl28xxu",))
        self.assertEqual(self.listed(engine, subscription)["devices"], [
            {"device": "RTLSDR1", "name": "RTL2838UHIDIR", "manufacturer": "Realtek", "drivers": ["usbfs"],
             "shared": False, "reads_as_index": False, "in_use": False, "radio": None, "found": True},
            {"device": "59196661", "name": "NESDR SMArt v5", "manufacturer": "Nooelec",
             "drivers": ["dvb_usb_rtl28xxu"], "shared": False, "reads_as_index": False, "in_use": False,
             "radio": None, "found": True},
        ])
        shutil.rmtree(self.root / "usb" / "1-1")
        self.assertEqual([device["device"] for device in self.listed(engine, subscription)["devices"]], ["59196661"])

    async def test_choosing_one_reads_it_by_its_serial_number_and_saves_it(self):
        plug_usb_device(self.root / "usb", "1-1", **BLOG)
        plug_usb_device(self.root / "usb", "1-2", **NESDR)
        engine = self.engine()
        subscription = engine.subscribe()
        self.assertEqual(engine.radios[0].source.kind, "synthetic")
        reply = self.listed(engine, subscription, choose="59196661")
        self.assertIsInstance(engine.radios[0].source, RtlSdrSource)
        self.assertEqual(engine.radios[0].source.reopen, {"kind": "rtl_sdr", "device": "59196661"})
        self.assertEqual([(device["device"], device["in_use"]) for device in reply["devices"]],
                         [("RTLSDR1", False), ("59196661", True)])
        # The one in use changes nothing.
        source = engine.radios[0].source
        self.listed(engine, subscription, choose="59196661")
        self.assertIs(engine.radios[0].source, source)
        engine.close()      # the settings are saved as Radiolack stops
        await asyncio.wait_for(engine.wait_closed(), timeout=5)
        self.assertEqual(self.store.load(SETTINGS_FILE)["source"], {"kind": "rtl_sdr", "device": "59196661"})

    async def test_each_receiver_names_the_radio_that_reads_it(self):
        plug_usb_device(self.root / "usb", "1-1", **BLOG)
        plug_usb_device(self.root / "usb", "1-2", **NESDR, drivers=("usbfs",))
        engine = self.engine()
        subscription = engine.subscribe()
        second = engine.add_radio({"kind": "rtl_sdr", "device": "59196661"})
        self.assertEqual([(device["device"], device["in_use"], device["radio"])
                          for device in self.listed(engine, subscription)["devices"]],
                         [("RTLSDR1", False, None), ("59196661", False, second.id)])
        with self.assertRaisesRegex(ValueError, "reads this device already"):
            engine.radios[0].configure_rtl_devices({"choose": "59196661"}, subscription)
        self.assertEqual(engine.radios[0].source.kind, "synthetic")

    async def test_refuses_a_receiver_not_plugged_in_or_that_rtl_tcp_cannot_tell(self):
        plug_usb_device(self.root / "usb", "1-1", **BLOG)
        plug_usb_device(self.root / "usb", "1-2", serial=None)
        plug_usb_device(self.root / "usb", "1-3", serial="00000001")      # index 1 for rtl_tcp
        plug_usb_device(self.root / "usb", "1-4", serial="ABC")
        plug_usb_device(self.root / "usb", "1-5", serial="ABC")
        engine = self.engine()
        subscription = engine.subscribe()
        for changes in ({"choose": "59196661"}, {"choose": "00000001"}, {"choose": "ABC"}, {"choose": ""}, {"choose": 3}, {"list": False}, {"list": True, "x": 1},
                        {}, {"open": "RTLSDR1"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                engine.radios[0].configure_rtl_devices(changes, subscription)
        self.assertEqual(engine.radios[0].source.kind, "synthetic")

    async def test_the_receiver_in_use_stays_listed_when_not_found(self):
        plug_usb_device(self.root / "usb", "1-1", **BLOG)
        engine = self.engine(source={"kind": "rtl_sdr", "device": "59196661"})
        subscription = engine.subscribe()
        self.assertEqual(self.listed(engine, subscription)["devices"][1], {
            "device": "59196661", "name": "", "manufacturer": "", "drivers": [], "shared": False,
            "reads_as_index": False, "in_use": True, "radio": 1, "found": False})

    async def test_the_first_receiver_by_index_is_the_one_plugged_in_alone(self):
        plug_usb_device(self.root / "usb", "1-1", **NESDR)
        engine = self.engine(source={"kind": "rtl_sdr", "device": "0"})
        subscription = engine.subscribe()
        devices = self.listed(engine, subscription)["devices"]
        self.assertEqual([(device["device"], device["in_use"]) for device in devices], [("59196661", True)])
        # With two, the index names neither: rtl_tcp's order need not be sysfs's.
        plug_usb_device(self.root / "usb", "1-2", **BLOG)
        devices = self.listed(engine, subscription)["devices"]
        self.assertEqual([(device["device"], device["in_use"], device["found"], device["reads_as_index"])
                          for device in devices],
                         [("59196661", False, True, False), ("RTLSDR1", False, True, False), ("0", True, False, True)])

    async def test_alone_plugged_in_it_starts_from_the_first_receivers_settings(self):
        plug_usb_device(self.root / "usb", "1-1", **NESDR)
        saved = {"source": {"centre_hz": 433_500_000.0, "ppm": -4.0}, "demod": {"mode": "AM"}}
        engine = self.engine(receivers={"rtl_sdr 0": saved})
        subscription = engine.subscribe()
        self.listed(engine, subscription, choose="59196661")
        state = engine.radios[0].state()
        self.assertEqual((state["centre_hz"], state["ppm"], engine.radios[0].demod_state()["mode"]),
                         (433_500_000.0, -4.0, "AM"))

    async def test_with_two_plugged_in_it_starts_from_its_own_settings(self):
        plug_usb_device(self.root / "usb", "1-1", **NESDR)
        plug_usb_device(self.root / "usb", "1-2", **BLOG)
        saved = {"source": {"centre_hz": 433_500_000.0, "ppm": -4.0}}
        engine = self.engine(receivers={"rtl_sdr 0": saved})
        subscription = engine.subscribe()
        self.listed(engine, subscription, choose="RTLSDR1")
        self.assertNotEqual(engine.radios[0].state()["centre_hz"], 433_500_000.0)

    async def test_an_engine_without_rtl_sdr_lists_nothing(self):
        engine = Engine(sources=type("Opener", (), {"kinds": ("synthetic",), "open": Sources().open})())
        self.addAsyncCleanup(engine.wait_closed)
        self.addCleanup(engine.close)
        self.assertFalse(engine.lists_rtl_devices)
        with self.assertRaisesRegex(ValueError, "does not list"):
            engine.radios[0].configure_rtl_devices({"list": True}, engine.subscribe())


if __name__ == "__main__":
    unittest.main()
