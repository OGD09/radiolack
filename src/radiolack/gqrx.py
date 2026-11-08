"""Gqrx remote-control client (receive-only use) and separate profile preparation.

RadioLack tunes Gqrx's VFO, sets mode, filter and squelch, and reads its
S-meter. Gqrx 2.15.9 does not export its spectrum through this protocol,
which is why FFT detection runs on the shared I/Q stream instead.
"""

from __future__ import annotations

import configparser
import math
from pathlib import Path
import socket
import sys
import time

from .errors import GqrxProtocolError, SquelchVerificationError
from .receiver import Endpoint

GQRX_USUAL_PROFILE = Path.home() / ".config" / "gqrx" / "default.conf"
# Gqrx profiles store the demodulator under its display name, which differs
# from the remote-control mode name for narrow FM. Gqrx 2.15 turns any other
# name into "Demod Off" ("Modulation 'FM' is unknown").
CONFIG_DEMOD_NAMES = {"AM": "AM", "FM": "Narrow FM"}
MIN_DBFS, MAX_DBFS = -150.0, 0.0
SQUELCH_TOLERANCE_DB = 0.11
PROVISIONAL_LIMITS_DBFS = (-149.0, -1.0)


class GqrxDisconnected(ConnectionError):
    """The remote-control connection was lost (recoverable: the monitor reconnects)."""


def _number(value: str, what: str) -> float:
    try:
        number = float(value)
    except ValueError:
        raise GqrxProtocolError(f"Gqrx returned a malformed {what}: {value!r}") from None
    if not math.isfinite(number):
        raise GqrxProtocolError(f"Gqrx returned a non-finite {what}: {value!r}")
    return number


def _integer(value: str, what: str) -> int:
    number = _number(value, what)
    if number != int(number):
        raise GqrxProtocolError(f"Gqrx returned a non-integer {what}: {value!r}")
    return int(number)


class GqrxControl:
    """Control Gqrx's VFO while the bridge supplies its whole I/Q window.

    On connection the current receiver settings are saved, then LNB offset 0,
    the profile's mode/filter, a muted squelch and DSP on are applied. ``close``
    restores the saved settings unless the connection is already dead.
    """

    def __init__(self, endpoint: Endpoint, *, mode: str, filter_hz: int, timeout: float = 2.0):
        self.mode = mode
        self.filter_hz = filter_hz
        self.saved: list[str] = []
        self.connection = socket.create_connection((endpoint.host, endpoint.port), timeout=timeout)
        self.stream = self.connection.makefile("rwb", buffering=0)
        try:
            frequency = _integer(self.command("f")[0], "frequency")
            mode_name, width = self.command("m", 2)
            squelch = _number(self.command("l SQL")[0], "squelch")
            offset = _integer(self.command("LNB_LO")[0], "LNB offset")
            dsp = _integer(self.command("u DSP")[0], "DSP state")
            self.saved = [f"LNB_LO {offset}", f"M {mode_name} {_integer(width, 'passband')}",
                          f"F {frequency}", f"L SQL {squelch}", f"U DSP {dsp}"]
            for command in ["LNB_LO 0", f"M {mode} {filter_hz}", "L SQL 0", "U DSP 1"]:
                self.command(command)
        except BaseException:
            self.close()
            raise

    def command(self, text: str, lines: int = 1) -> list[str]:
        self.stream.write((text + "\n").encode("ascii"))
        result = []
        for _ in range(lines):
            raw = self.stream.readline(4096)
            if not raw or not raw.endswith(b"\n"):
                raise GqrxDisconnected("Gqrx remote control disconnected")
            value = raw.decode("ascii", "replace").strip()
            if value.startswith("RPRT") and value != "RPRT 0":
                raise GqrxProtocolError(f"Gqrx rejected {text!r}: {value}")
            result.append(value)
        return result

    def frequency(self) -> int:
        return _integer(self.command("f")[0], "frequency")

    def mute(self):
        self.command("L SQL 0")

    def read_squelch(self) -> float:
        value = _number(self.command("l SQL")[0], "squelch")
        if not MIN_DBFS <= value <= MAX_DBFS:
            raise GqrxProtocolError(f"Gqrx returned an out-of-range squelch: {value}")
        return value

    def strength(self) -> float:
        """Gqrx S-meter in dBFS (integrated channel level, not an FFT bin)."""
        value = _number(self.command("l STRENGTH")[0], "signal level")
        if not MIN_DBFS <= value <= MAX_DBFS:
            raise GqrxProtocolError(f"Invalid Gqrx signal level: {value}")
        return value

    def gain_stages(self) -> dict[str, float]:
        values = {}
        for name in self.command("l ?")[0].split():
            if name.endswith("_GAIN"):
                values[name.removesuffix("_GAIN")] = _number(self.command("l " + name)[0], "gain")
        return values

    def apply_squelch(self, value: float) -> float:
        """Set the squelch, read it back and fail explicitly on a mismatch."""
        self.command(f"L SQL {value:.1f}")
        actual = self.read_squelch()
        if abs(actual - value) > SQUELCH_TOLERANCE_DB:
            raise SquelchVerificationError(
                f"Gqrx squelch verification failed: requested {value}, read {actual}")
        return actual

    def tune(self, frequency: int, squelch: float | None = None, *, settle_s: float = 0.25,
             readings: int = 3, interval_s: float = 0.08, provisional_offset_db: float = 6.0,
             sleep=time.sleep) -> float:
        """Mute, set mode/filter and frequency, then apply the squelch last.

        Without a memorized value the provisional threshold is set below the
        lowest of a few settled S-meter readings: it keeps the detected carrier
        audible without claiming a noise-floor measurement.
        """
        self.command("L SQL 0")
        self.command(f"M {self.mode} {self.filter_hz}")
        self.command(f"F {frequency}")
        if squelch is None:
            sleep(settle_s)
            levels = []
            for _ in range(readings):
                levels.append(self.strength())
                sleep(interval_s)
            low, high = PROVISIONAL_LIMITS_DBFS
            squelch = round(max(low, min(high, min(levels) - provisional_offset_db)), 1)
        return self.apply_squelch(squelch)

    def move_window(self, frequency: int, sample_rate: int):
        """Force a hardware retune instead of just moving the VFO cursor.

        Gqrx keeps small remote frequency changes inside the existing I/Q
        window. A muted staging hop beyond that window followed by the focus
        frequency forces the actual receiver and panorama to move together.
        """
        self.command("L SQL 0")
        self.command(f"F {frequency + 2 * sample_rate}")
        self.command(f"F {frequency}")

    def close(self, restore: bool = True):
        try:
            if restore:
                for command in self.saved:
                    self.command(command)
        except (OSError, ValueError, GqrxProtocolError):
            print("Could not restore Gqrx settings; check them manually.", file=sys.stderr)
        finally:
            self.stream.close()
            self.connection.close()


def prepare_gqrx_profile(path, *, bridge: Endpoint, remote: Endpoint, sample_rate: int,
                         center_hz: int, mode: str, filter_hz: int,
                         base: Path | None = GQRX_USUAL_PROFILE) -> Path:
    """Create a separate Gqrx profile for the bridge; never overwrite any file.

    The usual Gqrx profile is only read (as a base for display preferences)
    and is never a valid output path.
    """
    target = Path(path).expanduser().absolute()
    if target == GQRX_USUAL_PROFILE.absolute() or (base is not None and target == Path(base).absolute()):
        raise ValueError(f"Refusing to write Gqrx's usual profile {target}; choose a separate file")
    if mode not in CONFIG_DEMOD_NAMES:
        raise ValueError(f"No Gqrx profile demodulator for mode {mode!r}; "
                         f"supported: {', '.join(CONFIG_DEMOD_NAMES)}")
    config = configparser.ConfigParser(interpolation=None)
    config.optionxform = str
    if base is not None and Path(base).exists():
        try:
            config.read(base, encoding="utf-8")
        except (configparser.Error, UnicodeDecodeError) as error:
            raise ValueError(f"Cannot parse the base Gqrx profile {base}: {error}") from None
    half = filter_hz // 2
    settings = {
        "General": {"configversion": "3", "crashed": "false"},
        "input": {"device": f'"rtl_tcp={bridge}"', "sample_rate": str(sample_rate),
                  "bandwidth": "0", "lnb_lo": "0", "frequency": str(center_hz)},
        "receiver": {"demod": CONFIG_DEMOD_NAMES[mode], "filter_low_cut": str(-half),
                     "filter_high_cut": str(half), "offset": "0"},
        "fft": {"fft_center": "0"},
        "remote_control": {"allowed_hosts": "127.0.0.1" if remote.host == "localhost" else remote.host,
                           "port": str(remote.port), "enabled": "true"},
    }
    for section, values in settings.items():
        if not config.has_section(section):
            config.add_section(section)
        for key, value in values.items():
            config.set(section, key, value)
    # A direct-USB profile's serialized gain map may not match rtl_tcp's stages.
    config.remove_option("input", "gains")
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("x", encoding="utf-8") as stream:
        config.write(stream, space_around_delimiters=False)
    return target
