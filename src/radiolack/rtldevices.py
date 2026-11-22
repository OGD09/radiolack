"""The RTL-SDR receivers plugged into this computer, listed from Linux's
sysfs without opening any of them.

Each USB device under ``/sys/bus/usb/devices`` whose vendor and product IDs
are among those that librtlsdr opens (``KNOWN_DEVICES``) is an RTL-SDR
receiver: its manufacturer, product and serial number are the strings its
USB descriptor gave the kernel, and the drivers bound to its interfaces say
who holds it: ``usbfs`` when a program opened it through libusb (rtl_tcp,
Gqrx, or Radiolack itself for the source in use), ``dvb_usb_rtl28xxu``
when the kernel's DVB driver took it. Nothing here calls librtlsdr,
rtl_test, rtl_eeprom, rtl_tcp or SoapySDR, which would open the receivers;
reading these files never disturbs one that streams.

rtl_tcp's ``-d`` reads its text as an index first, as C's strtol in base 0
does (``reads_as_index``: "00000001" is index 1 with two receivers), then as
a serial number, opening the first receiver with it in librtlsdr's own
order, which need not be sysfs's. A receiver whose serial number reads as
an index, or which shares its serial number with another, cannot be named
by it: the list marks them so (``RtlDevice.reads_as_index`` and
``RtlDevice.shared``) rather than guessing an index. The functions'
behaviour comes from ``verbose_device_search`` in rtl-sdr 0.6.0's
src/convenience/convenience.c, unchanged in 2.0.2.

``KNOWN_DEVICES`` is librtlsdr's table of RTL2832U devices,
``known_devices[]`` in src/librtlsdr.c of rtl-sdr 0.6.0 (the version that
Debian 12 ships), unchanged in rtl-sdr 2.0.2. The licence notice of that
file, which this copy keeps:

    rtl-sdr, turns your Realtek RTL2832 based DVB dongle into a SDR receiver
    Copyright (C) 2012-2014 by Steve Markgraf <steve@steve-m.de>
    Copyright (C) 2012 by Dimitri Stolnikov <horiz0n@gmx.net>

    This program is free software: you can redistribute it and/or modify
    it under the terms of the GNU General Public License as published by
    the Free Software Foundation, either version 2 of the License, or
    (at your option) any later version.

    This program is distributed in the hope that it will be useful,
    but WITHOUT ANY WARRANTY; without even the implied warranty of
    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
    GNU General Public License for more details.

This copy is part of Radiolack, under the GNU General Public License,
version 3 or later, as the "or later" above allows.
"""

from __future__ import annotations

import dataclasses
import os
import sys
from dataclasses import dataclass
from pathlib import Path

USB_DEVICES = Path("/sys/bus/usb/devices")

# (idVendor, idProduct): name, from librtlsdr's known_devices[] (module docstring).
KNOWN_DEVICES: dict[tuple[int, int], str] = {
    (0x0BDA, 0x2832): "Generic RTL2832U",
    (0x0BDA, 0x2838): "Generic RTL2832U OEM",
    (0x0413, 0x6680): "DigitalNow Quad DVB-T PCI-E card",
    (0x0413, 0x6F0F): "Leadtek WinFast DTV Dongle mini D",
    (0x0458, 0x707F): "Genius TVGo DVB-T03 USB dongle (Ver. B)",
    (0x0CCD, 0x00A9): "Terratec Cinergy T Stick Black (rev 1)",
    (0x0CCD, 0x00B3): "Terratec NOXON DAB/DAB+ USB dongle (rev 1)",
    (0x0CCD, 0x00B4): "Terratec Deutschlandradio DAB Stick",
    (0x0CCD, 0x00B5): "Terratec NOXON DAB Stick - Radio Energy",
    (0x0CCD, 0x00B7): "Terratec Media Broadcast DAB Stick",
    (0x0CCD, 0x00B8): "Terratec BR DAB Stick",
    (0x0CCD, 0x00B9): "Terratec WDR DAB Stick",
    (0x0CCD, 0x00C0): "Terratec MuellerVerlag DAB Stick",
    (0x0CCD, 0x00C6): "Terratec Fraunhofer DAB Stick",
    (0x0CCD, 0x00D3): "Terratec Cinergy T Stick RC (Rev.3)",
    (0x0CCD, 0x00D7): "Terratec T Stick PLUS",
    (0x0CCD, 0x00E0): "Terratec NOXON DAB/DAB+ USB dongle (rev 2)",
    (0x1554, 0x5020): "PixelView PV-DT235U(RN)",
    (0x15F4, 0x0131): "Astrometa DVB-T/DVB-T2",
    (0x15F4, 0x0133): "HanfTek DAB+FM+DVB-T",
    (0x185B, 0x0620): "Compro Videomate U620F",
    (0x185B, 0x0650): "Compro Videomate U650F",
    (0x185B, 0x0680): "Compro Videomate U680F",
    (0x1B80, 0xD393): "GIGABYTE GT-U7300",
    (0x1B80, 0xD394): "DIKOM USB-DVBT HD",
    (0x1B80, 0xD395): "Peak 102569AGPK",
    (0x1B80, 0xD397): "KWorld KW-UB450-T USB DVB-T Pico TV",
    (0x1B80, 0xD398): "Zaapa ZT-MINDVBZP",
    (0x1B80, 0xD39D): "SVEON STV20 DVB-T USB & FM",
    (0x1B80, 0xD3A4): "Twintech UT-40",
    (0x1B80, 0xD3A8): "ASUS U3100MINI_PLUS_V2",
    (0x1B80, 0xD3AF): "SVEON STV27 DVB-T USB & FM",
    (0x1B80, 0xD3B0): "SVEON STV21 DVB-T USB & FM",
    (0x1D19, 0x1101): "Dexatek DK DVB-T Dongle (Logilink VG0002A)",
    (0x1D19, 0x1102): "Dexatek DK DVB-T Dongle (MSI DigiVox mini II V3.0)",
    (0x1D19, 0x1103): "Dexatek Technology Ltd. DK 5217 DVB-T Dongle",
    (0x1D19, 0x1104): "MSI DigiVox Micro HD",
    (0x1F4D, 0xA803): "Sweex DVB-T USB",
    (0x1F4D, 0xB803): "GTek T803",
    (0x1F4D, 0xC803): "Lifeview LV5TDeluxe",
    (0x1F4D, 0xD286): "MyGica TD312",
    (0x1F4D, 0xD803): "PROlectrix DV107669",
}

MAX_DEVICES = 32        # receivers listed at most
_MAX_TEXT = 64          # characters kept of a descriptor's string; rtl_tcp's -d takes up to 64 here too
_READ_BYTES = 256       # bytes read of a sysfs attribute


@dataclass(frozen=True)
class RtlDevice:
    """One RTL-SDR receiver plugged in, as sysfs describes it.

    ``serial`` is its USB serial number, "" when it has none (rtl_tcp's
    ``-d`` cannot then name it); ``name`` its product string, or librtlsdr's
    name for its IDs; ``drivers`` the kernel drivers bound to its
    interfaces, such as ``usbfs`` (a program holds it) or
    ``dvb_usb_rtl28xxu``; ``shared`` that another receiver listed has the
    same serial number; ``reads_as_index`` that rtl_tcp's ``-d`` would read
    its serial number as an index among those listed."""

    serial: str
    manufacturer: str
    name: str
    drivers: tuple[str, ...]
    shared: bool = False
    reads_as_index: bool = False

    @property
    def choosable(self) -> bool:
        """Whether rtl_tcp's ``-d`` opens it, and it alone, by its serial number."""
        return bool(self.serial) and not self.shared and not self.reads_as_index


def list_devices(root: Path = USB_DEVICES) -> list[RtlDevice] | None:
    """The RTL-SDR receivers under ``root``, in the bus's order, at most
    MAX_DEVICES; None when there is no such folder, on a system other than
    Linux for instance. A device whose files cannot be read is left out:
    one unplugged while it is read, for instance."""
    try:
        names = sorted((entry.name for entry in os.scandir(root) if ":" not in entry.name), key=_bus_order)
    except OSError:
        return None
    found: list[RtlDevice] = []
    for name in names:
        device = _read_device(root / name)
        if device is not None:
            found.append(device)
            if len(found) == MAX_DEVICES:
                break
    counts: dict[str, int] = {}
    for device in found:
        if device.serial:
            counts[device.serial] = counts.get(device.serial, 0) + 1
    return [dataclasses.replace(device, shared=counts.get(device.serial, 0) > 1,
                                reads_as_index=reads_as_index(device.serial, len(found)))
            if device.serial else device for device in found]


def reads_as_index(text: str, count: int) -> bool:
    """Whether rtl_tcp's ``-d TEXT`` takes TEXT as the index of one of
    ``count`` receivers rather than as a serial number: C's strtol in base 0
    reads it whole (decimal, octal after a leading 0, hexadecimal after
    0x, after spaces and a sign) as a number from 0 to ``count`` - 1."""
    rest = text.lstrip(" \t\n\v\f\r")
    negative = rest[:1] == "-"
    if rest[:1] in "+-":
        rest = rest[1:]
    if rest[:2].lower() == "0x" and len(rest) > 2:
        base, digits = 16, rest[2:]
    elif rest[:1] == "0":
        base, digits = 8, rest
    else:
        base, digits = 10, rest
    try:
        value = int(digits, base) if digits.isascii() and digits.isalnum() else None
    except ValueError:
        value = None
    if value is None:
        return text == "" and count > 0     # strtol reads nothing, which is 0
    if negative:
        value = -value
    return 0 <= value < count


def _read_device(path: Path) -> RtlDevice | None:
    """The receiver at ``path``, or None when it is no RTL-SDR or cannot be read."""
    vendor, product = _read_id(path / "idVendor"), _read_id(path / "idProduct")
    if vendor is None or product is None:
        return None
    known = KNOWN_DEVICES.get((vendor, product))
    if known is None:
        return None
    serial = _read_text(path / "serial")
    return RtlDevice(serial=serial, manufacturer=_read_text(path / "manufacturer"),
                     name=_read_text(path / "product") or known, drivers=_drivers(path))


def _drivers(path: Path) -> tuple[str, ...]:
    """The drivers bound to the interfaces of the device at ``path``, each once, in order."""
    drivers: list[str] = []
    try:
        interfaces = sorted(entry.name for entry in os.scandir(path) if entry.name.startswith(f"{path.name}:"))
    except OSError:
        return ()
    for interface in interfaces:
        try:
            driver = _clean(os.path.basename(os.readlink(path / interface / "driver")))
        except OSError:
            continue        # no driver bound
        if driver and driver not in drivers:
            drivers.append(driver)
    return tuple(drivers)


def _read_text(path: Path) -> str:
    """A sysfs attribute's text, printable and at most _MAX_TEXT characters; "" when missing."""
    try:
        with open(path, "rb") as file:
            data = file.read(_READ_BYTES)
    except OSError:
        return ""
    return _clean(data.decode("utf-8", "replace"))


def _clean(text: str) -> str:
    return "".join(character for character in text.strip() if character.isprintable())[:_MAX_TEXT].strip()


def _read_id(path: Path) -> int | None:
    """A four-digit hexadecimal ID, such as idVendor's, or None."""
    text = _read_text(path)
    if len(text) != 4:
        return None
    try:
        return int(text, 16)
    except ValueError:
        return None


def _bus_order(name: str) -> tuple[int, ...]:
    """Sort key of a device's name, such as "3-1.4": bus, then each port; names
    that are not of that form, such as "usb1" (a root hub), last."""
    try:
        bus, ports = name.split("-", 1)
        return (int(bus), *(int(port) for port in ports.split(".")))
    except ValueError:
        return (sys.maxsize,)
