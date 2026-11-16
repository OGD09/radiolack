"""The two ways rtl-sdr's driver programs the PLL of an R820T or R828D
tuner, ported to predict the frequency that the tuner truly sets for one
asked: the tests' fake rtl_tcp and tools/measure_pll.py use them, nothing
that Radiolack installs does.

librtlsdr asks the tuner for a frequency F; the tuner's oscillator runs at
F plus the intermediate frequency (IF), and the RTL2832 brings the IF down
to 0 Hz with its own mixer, set once for the IF. The PLL makes the
oscillator from the crystal X (28.8 MHz on an R820T, and on the RTL-SDR
Blog V4's R828D): a voltage-controlled oscillator (VCO) at
2X × (nint + sdm / 65536), divided by mix_div, a power of two from 2 to
64 that puts the VCO between 1.77 and 3.54 GHz. Whatever the VCO cannot
reach exactly stays: the tuner sits that far from F, and nothing makes up
for it, so a carrier moves by as much in the samples.

- ``truncating``: rtl-sdr up to 2.0.1 (0.6.0 on Debian 12) and the RTL-SDR
  Blog fork. It takes the VCO's remainder over 2X × nint in whole kHz,
  truncated, and builds sdm bit by bit, taking a bit only while the
  remainder is strictly greater than the bit's weight, itself truncated to
  the kHz. It is exact only when the VCO is a multiple of 2X: on any other
  frequency, its last bit meets a remainder equal to its weight and is
  left for the smaller ones, which add up to less.
- ``rounding``: rtl-sdr 2.0.2 and later: the VCO over 2X, rounded to the
  nearest 65536th. It is within 2X / 65536 / 2, 439 Hz, of the VCO asked,
  so within 439 Hz / mix_div of the frequency asked, and exact when the
  oscillator is a multiple of 112.5 kHz on a 28.8 MHz crystal.

The IF follows the tuner's bandwidth, which librtlsdr sets to the sample
rate it truly runs at whenever the sample rate changes: 3.57 MHz above
2.43 MS/s, 1.815 MHz at 2.4 MS/s, 1.7 MHz at 1.024 MS/s (``intermediate_hz``).

Ported from rtl-sdr, read on its GitHub mirrors on 2026-11-16:

- osmocom/rtl-sdr, ``src/tuner_r82xx.c`` (``r82xx_set_pll``,
  ``r82xx_set_bandwidth``, ``r82xx_set_freq``) and ``src/librtlsdr.c``
  (``rtlsdr_set_sample_rate``, ``rtlsdr_get_xtal_freq``), at the tags
  v0.6.0 (1f0eafe60445339703903af6d8814ffab7e73784) and v2.0.1
  (420086af84d7eaaf98ff948cd11fea2cae71734a), whose ``r82xx_set_pll`` is
  the same: ``truncating``; v2.0.2 (619ac3186ea0ffc092615e1f59f7397e5e6f668c)
  and 2.0.3 on master (797f8143266d983c56d8f35d2d442527529dd8a5), which
  carry Oliver Jowett's commit "r82xx: improve tuner precision"
  (f5978e88713e2ac61030ca6c14af680830d886b2): ``rounding``.
- rtlsdrblog/rtl-sdr-blog, the same files on master
  (aed0ea19f3a273370a13c9009b96313c75d54c7b): its ``r82xx_set_pll``
  computes as v2.0.1 does, only its VCO current differs: ``truncating``.
- ``r82xx_set_bandwidth`` and ``rtlsdr_set_sample_rate`` are the same in
  all of them.

Left out, as they do not move the frequency: the registers written, the
VCO's fine tune, which changes the divider's register but not the VCO
computed, the lock check, and the RTL2832's mixer, whose own rounding is
the same for every centre frequency. The RTL-SDR Blog V4's upconversion
below 28.8 MHz is left out too: the measure runs above it.

The licence notice of the files ported, which this port keeps:

    Rafael Micro R820T/R828D driver

    Copyright (C) 2013 Mauro Carvalho Chehab <mchehab@redhat.com>
    Copyright (C) 2013 Steve Markgraf <steve@steve-m.de>

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

This port is part of Radiolack, under the GNU General Public License,
version 3 or later, as the "or later" above allows.
"""

from __future__ import annotations

from collections.abc import Callable

XTAL_HZ = 28_800_000        # the crystal of an R820T, and of the RTL-SDR Blog V4's R828D
VCO_MIN_KHZ = 1_770_000     # the VCO's range, in kHz, as the driver checks it
VCO_MAX_KHZ = 2 * VCO_MIN_KHZ
SDM_STEPS = 65_536          # the PLL's fraction: sdm / 65536

# r82xx_set_bandwidth: the IF filter's widths, and the high-pass steps.
_LOW_PASS_HZ = (1_700_000, 1_600_000, 1_550_000, 1_450_000, 1_200_000, 900_000, 700_000, 550_000, 450_000,
                350_000)
_HIGH_PASS_1_HZ = 350_000
_HIGH_PASS_2_HZ = 380_000


def mix_divider(lo_hz: int) -> int:
    """The divider that puts the VCO in its range for an oscillator at
    ``lo_hz``; ValueError when none does, where the driver's PLL cannot
    lock."""
    freq_khz = (lo_hz + 500) // 1000
    mix_div = 2
    while mix_div <= 64:
        if VCO_MIN_KHZ <= freq_khz * mix_div < VCO_MAX_KHZ:
            return mix_div
        mix_div <<= 1
    raise ValueError(f"no divider puts the VCO in its range for an oscillator at {lo_hz} Hz")


def _checked(nint: int, vco_hz: int) -> int:
    """``nint``, if the driver takes it: it refuses one above 63, and the
    registers of one below 13 would wrap."""
    if not 13 <= nint <= 63:
        raise ValueError(f"no valid PLL values for a VCO at {vco_hz} Hz")
    return nint


def truncating_lo_hz(lo_hz: int, xtal_hz: int = XTAL_HZ) -> float:
    """The oscillator that rtl-sdr up to 2.0.1, and the RTL-SDR Blog fork,
    truly set for one asked at ``lo_hz``, in units of the crystal that the
    driver takes to run at ``xtal_hz``."""
    mix_div = mix_divider(lo_hz)
    vco_hz = lo_hz * mix_div
    nint = _checked(vco_hz // (2 * xtal_hz), vco_hz)
    vco_fra_khz = (vco_hz - 2 * xtal_hz * nint) // 1000
    pll_ref_khz = (xtal_hz + 500) // 1000
    n_sdm, sdm = 2, 0
    while vco_fra_khz > 1:
        step_khz = 2 * pll_ref_khz // n_sdm
        if vco_fra_khz > step_khz:
            sdm += 32768 // (n_sdm // 2)
            vco_fra_khz -= step_khz
            if n_sdm >= 0x8000:
                break
        n_sdm = (n_sdm << 1) & 0xFFFF       # uint16_t, as in the driver
    return 2 * xtal_hz * (nint + sdm / SDM_STEPS) / mix_div


def rounding_lo_hz(lo_hz: int, xtal_hz: int = XTAL_HZ) -> float:
    """The oscillator that rtl-sdr 2.0.2 and later truly set for one asked
    at ``lo_hz``, in units of the crystal that the driver takes to run at
    ``xtal_hz``."""
    mix_div = mix_divider(lo_hz)
    vco_hz = lo_hz * mix_div
    vco_div = (xtal_hz + SDM_STEPS * vco_hz) // (2 * xtal_hz)
    _checked(vco_div // SDM_STEPS, vco_hz)
    return 2 * xtal_hz * vco_div / SDM_STEPS / mix_div


COMPUTATIONS: dict[str, Callable[[int, int], float]] = {
    "truncating": truncating_lo_hz,
    "rounding": rounding_lo_hz,
}


def real_rate_hz(rate_hz: int, rtl_xtal_hz: int = XTAL_HZ) -> int:
    """The sample rate that the RTL2832 truly runs at for one asked, as
    librtlsdr computes it and hands it to the tuner as its bandwidth."""
    ratio = int(rtl_xtal_hz * 2.0**22 / rate_hz) & 0x0FFFFFFC
    real_ratio = ratio | ((ratio & 0x08000000) << 1)
    return int(rtl_xtal_hz * 2.0**22 / real_ratio)


def intermediate_hz(rate_hz: int) -> int:
    """The tuner's IF at the sample rate ``rate_hz``: r82xx_set_bandwidth
    for the bandwidth that librtlsdr gives it, the rate it truly runs at."""
    bandwidth = real_rate_hz(rate_hz)
    if bandwidth > 6_000_000:
        return 4_570_000
    if bandwidth > _LOW_PASS_HZ[0] + _HIGH_PASS_1_HZ + _HIGH_PASS_2_HZ:
        return 3_570_000
    int_freq, real_bw = 2_300_000, 0
    if bandwidth > _LOW_PASS_HZ[0] + _HIGH_PASS_1_HZ:
        bandwidth -= _HIGH_PASS_2_HZ
        int_freq += _HIGH_PASS_2_HZ
        real_bw += _HIGH_PASS_2_HZ
    if bandwidth > _LOW_PASS_HZ[0]:
        bandwidth -= _HIGH_PASS_1_HZ
        int_freq += _HIGH_PASS_1_HZ
        real_bw += _HIGH_PASS_1_HZ
    index = next((i for i, width in enumerate(_LOW_PASS_HZ) if bandwidth > width), len(_LOW_PASS_HZ)) - 1
    assert index >= 0, "the steps above leave the bandwidth within the narrowest filters"
    real_bw += _LOW_PASS_HZ[index]
    return int_freq - real_bw // 2


def driver_xtal_hz(correction_ppm: int) -> int:
    """The crystal frequency that librtlsdr gives the tuner's driver with
    the correction ``correction_ppm`` (rtl_tcp's command 0x05)."""
    return int(XTAL_HZ * (1.0 + correction_ppm / 1e6))


def oscillator_hz(frequency_hz: int, rate_hz: int) -> int:
    """The oscillator that librtlsdr asks of the tuner for ``frequency_hz``
    at the sample rate ``rate_hz``."""
    return frequency_hz + intermediate_hz(rate_hz)


def tuned_hz(computation: str, frequency_hz: int, rate_hz: int, correction_ppm: int = 0) -> float:
    """The frequency that the tuner truly brings to 0 Hz when librtlsdr is
    asked for ``frequency_hz`` at ``rate_hz`` with ``correction_ppm``, by
    the driver's ``computation``, in units of the crystal as the driver
    takes it to run: a crystal that truly runs elsewhere scales it, as it
    scales every frequency."""
    intermediate = intermediate_hz(rate_hz)
    lo_hz = COMPUTATIONS[computation](frequency_hz + intermediate, driver_xtal_hz(correction_ppm))
    return lo_hz - intermediate
