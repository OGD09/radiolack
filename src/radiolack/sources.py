"""The sources Radiolack opens by kind: those the Receiver menu switches
between, and the one used last, which Radiolack opens again at start.

Each source tells how to open its receiver again (``Source.reopen``): its
kind and, for an RTL-SDR on this computer, its device, for a SoapySDR
device, its device arguments, with its serial number once found. The other
settings are saved per receiver by the engine, such as the address of an
rtl_tcp server.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

from .engine import Source
from .rtldevices import USB_DEVICES, RtlDevice, list_devices
from .rtltcp import RTL_TCP_PROGRAM, RtlSdrSource, RtlTcpSource
from .soapy import SoapySource
from .synthetic import SyntheticBand

DEFAULT_DEVICE = "0"    # the first RTL-SDR receiver plugged in
_MAX_DEVICE = 64        # characters in a receiver's index or serial number


class Sources:
    """Opens the simulated band, an RTL-SDR on this computer (through an
    rtl_tcp it runs: ``rtl_tcp_program``, a fake one in tests), an rtl_tcp
    server or a SoapySDR device, which only its device arguments open:
    ``--soapy``, the Receiver menu's list of the devices found or its
    arguments field gives them first, and they are saved. The first of
    ``kinds`` is the default. It lists the RTL-SDR receivers plugged in
    from the sysfs folder ``usb_devices`` (rtldevices.py), a fake tree in
    tests."""

    kinds = ("synthetic", "rtl_sdr", "rtl_tcp", "soapy")

    def __init__(self, *, rtl_tcp_program: Sequence[str] = (RTL_TCP_PROGRAM,),
                 usb_devices: Path = USB_DEVICES) -> None:
        self._program = tuple(rtl_tcp_program)
        self._usb_devices = usb_devices

    def recipe(self) -> dict[str, object]:
        """How to make these sources again in another process, as JSON
        values: ``from_recipe`` takes them (radioproc.py)."""
        return {"rtl_tcp_program": list(self._program), "usb_devices": str(self._usb_devices)}

    @classmethod
    def from_recipe(cls, recipe: Mapping[str, object]) -> Sources:
        """Sources as ``recipe`` describes them. Raises ValueError for a
        recipe that ``recipe`` did not give."""
        program, usb_devices = recipe.get("rtl_tcp_program"), recipe.get("usb_devices")
        if (not isinstance(program, list) or not program or not all(isinstance(part, str) for part in program)
                or not isinstance(usb_devices, str)):
            raise ValueError("not a recipe for sources")
        return cls(rtl_tcp_program=program, usb_devices=Path(usb_devices))

    def rtl_devices(self) -> list[RtlDevice] | None:
        """The RTL-SDR receivers plugged into this computer, none opened;
        None where sysfs cannot tell (rtldevices.list_devices)."""
        return list_devices(self._usb_devices)

    def open(self, reopen: Mapping[str, object]) -> Source:
        """A new source from what ``reopen`` gave of another, such as
        {"kind": "rtl_sdr", "device": "0"}. It reads nothing until the engine
        asks. Raises ValueError for an unknown kind or device."""
        kind = reopen.get("kind")
        if kind == "synthetic":
            return SyntheticBand()
        if kind == "rtl_sdr":
            device = reopen.get("device", DEFAULT_DEVICE)
            if not isinstance(device, str) or not device.strip() or len(device) > _MAX_DEVICE:
                raise ValueError(f"not a receiver's index or serial number: {str(device)[:_MAX_DEVICE]!r}")
            return RtlSdrSource(device, program=self._program)
        if kind == "rtl_tcp":
            return RtlTcpSource()       # at rtl_tcp's default address until the saved one applies
        if kind == "soapy":
            args = reopen.get("args")
            if args is None:
                raise ValueError("no SoapySDR device chosen yet: choose one in the Receiver menu, or start "
                                 "Radiolack with --soapy and its device arguments, such as --soapy driver=rtlsdr")
            if not isinstance(args, str):
                raise ValueError("a SoapySDR device's arguments must be text")
            return SoapySource(args)
        raise ValueError(f"unknown kind of source: {str(kind)[:_MAX_DEVICE]!r}")
