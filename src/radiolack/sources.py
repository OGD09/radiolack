"""The sources Radiolack opens by kind: those the Source panel switches
between, and the one used last, which Radiolack opens again at start.

Each source tells how to open its receiver again (``Source.reopen``): its
kind and, for an RTL-SDR on this computer, its device. The other settings
are saved per receiver by the engine, such as the address of an rtl_tcp
server.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from .engine import Source
from .rtltcp import RTL_TCP_PROGRAM, RtlSdrSource, RtlTcpSource
from .synthetic import SyntheticBand

DEFAULT_DEVICE = "0"    # the first RTL-SDR receiver plugged in
_MAX_DEVICE = 64        # characters in a receiver's index or serial number


class Sources:
    """Opens the simulated band, an RTL-SDR on this computer (through an
    rtl_tcp it runs: ``rtl_tcp_program``, a fake one in tests) or an rtl_tcp
    server. The first of ``kinds`` is the default."""

    kinds = ("synthetic", "rtl_sdr", "rtl_tcp")

    def __init__(self, *, rtl_tcp_program: Sequence[str] = (RTL_TCP_PROGRAM,)) -> None:
        self._program = tuple(rtl_tcp_program)

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
        raise ValueError(f"unknown kind of source: {str(kind)[:_MAX_DEVICE]!r}")
