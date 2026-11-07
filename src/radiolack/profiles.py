"""Band profiles: band limits, demodulation, channel grid and listen criteria.

Profiles are TOML files. Built-in profiles ship inside the package
(``builtin_profiles/``); any other file can be passed by path. Unknown keys
are rejected so that a typo never silently falls back to a default.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from importlib import resources
import math
from pathlib import Path
import tomllib

from .detection import ChannelGrid

DEFAULT_PROFILE = "airband-8k33"
# Gqrx remote-control mode names with a symmetric passband (-width/2, +width/2).
SUPPORTED_MODES = ("AM", "FM")
_BUILTIN_DIR = "builtin_profiles"

_SCHEMA = {
    None: {"name", "description", "band", "demodulation", "channels", "detection",
           "listening", "exploration"},
    "band": {"low_hz", "high_hz"},
    "demodulation": {"mode", "filter_hz"},
    "channels": {"origin_hz", "spacing_hz", "snap_tolerance_hz"},
    "detection": {"margin_db", "confirm_observations"},
    "listening": {"seconds", "min_active_seconds", "min_observed_seconds", "cooldown_seconds"},
    "exploration": {"window_seconds", "initial_center_hz"},
}


class ProfileError(ValueError):
    """Invalid, unreadable or unknown profile."""


def _check_range(name, value, low, high, *, integer=False):
    kinds = (int,) if integer else (int, float)
    if isinstance(value, bool) or not isinstance(value, kinds) or not math.isfinite(value):
        raise ProfileError(f"{name} must be a {'whole ' if integer else ''}number")
    if not low <= value <= high:
        raise ProfileError(f"{name} must be between {low:g} and {high:g} (got {value:g})")


@dataclass(frozen=True)
class Profile:
    """Validated band profile; ``dataclasses.replace`` re-runs validation."""

    name: str
    description: str
    grid: ChannelGrid
    mode: str
    filter_hz: int
    margin_db: float
    confirm_observations: int
    listen_seconds: float
    min_active_seconds: float
    min_observed_seconds: float
    cooldown_seconds: float
    window_seconds: float
    initial_center_hz: int
    source: str = "built-in"

    def __post_init__(self):
        if not self.name or not isinstance(self.name, str):
            raise ProfileError("Profile name must be a non-empty string")
        if self.mode not in SUPPORTED_MODES:
            raise ProfileError(f"Mode must be one of {', '.join(SUPPORTED_MODES)} (got {self.mode!r})")
        _check_range("filter_hz", self.filter_hz, 1000, 200000, integer=True)
        _check_range("margin_db", self.margin_db, 3, 40)
        _check_range("confirm_observations", self.confirm_observations, 1, 10, integer=True)
        _check_range("listening seconds", self.listen_seconds, 2, 120)
        _check_range("min_active_seconds", self.min_active_seconds, 0.5, self.listen_seconds)
        if self.min_active_seconds >= self.listen_seconds:
            raise ProfileError("min_active_seconds must be shorter than the listening period")
        _check_range("min_observed_seconds", self.min_observed_seconds, 0.5, self.listen_seconds)
        _check_range("cooldown_seconds", self.cooldown_seconds, 1, 3600)
        _check_range("window_seconds", self.window_seconds, 0.5, 30)
        _check_range("initial_center_hz", self.initial_center_hz, 1, 10**10, integer=True)
        if not self.grid.contains(self.initial_center_hz):
            raise ProfileError("initial_center_hz must lie inside the band")

    @property
    def summary(self) -> str:
        return (f"{self.grid.low_hz / 1e6:.3f}-{self.grid.high_hz / 1e6:.3f} MHz | "
                f"{self.mode} {self.filter_hz / 1000:g} kHz | grid {self.grid.spacing_label} Hz")


def _spacing(value) -> Fraction:
    try:
        if isinstance(value, str):
            spacing = Fraction(value.strip())
        elif isinstance(value, int) and not isinstance(value, bool):
            spacing = Fraction(value)
        elif isinstance(value, float) and math.isfinite(value):
            spacing = Fraction(str(value))
        else:
            raise ValueError
    except (ValueError, ZeroDivisionError):
        raise ProfileError("channels.spacing_hz must be a number or a fraction such as \"25000/3\"") from None
    if spacing <= 0:
        raise ProfileError("channels.spacing_hz must be positive")
    return spacing


def parse_profile(data: dict, source: str) -> Profile:
    """Build a profile from parsed TOML, rejecting unknown or missing keys."""
    if not isinstance(data, dict):
        raise ProfileError("A profile must be a TOML table")
    unknown = set(data) - _SCHEMA[None]
    if unknown:
        raise ProfileError(f"Unknown profile keys: {', '.join(sorted(unknown))}")
    for table, keys in _SCHEMA.items():
        if table is None:
            continue
        section = data.get(table)
        if not isinstance(section, dict):
            raise ProfileError(f"Missing [{table}] table")
        extra, missing = set(section) - keys, keys - set(section)
        if extra:
            raise ProfileError(f"Unknown keys in [{table}]: {', '.join(sorted(extra))}")
        if missing:
            raise ProfileError(f"Missing keys in [{table}]: {', '.join(sorted(missing))}")
    band, demodulation, channels = data["band"], data["demodulation"], data["channels"]
    detection, listening, exploration = data["detection"], data["listening"], data["exploration"]
    for name, value in (("band.low_hz", band["low_hz"]), ("band.high_hz", band["high_hz"]),
                        ("channels.origin_hz", channels["origin_hz"])):
        _check_range(name, value, 1, 10**10, integer=True)
    _check_range("channels.snap_tolerance_hz", channels["snap_tolerance_hz"], 0, 10**6)
    try:
        grid = ChannelGrid(band["low_hz"], band["high_hz"], channels["origin_hz"],
                           _spacing(channels["spacing_hz"]), channels["snap_tolerance_hz"])
    except ValueError as error:
        raise ProfileError(str(error)) from None
    name, description = data.get("name"), data.get("description", "")
    if not isinstance(description, str):
        raise ProfileError("description must be a string")
    return Profile(
        name=name, description=description, grid=grid,
        mode=demodulation["mode"], filter_hz=demodulation["filter_hz"],
        margin_db=detection["margin_db"], confirm_observations=detection["confirm_observations"],
        listen_seconds=listening["seconds"], min_active_seconds=listening["min_active_seconds"],
        min_observed_seconds=listening["min_observed_seconds"],
        cooldown_seconds=listening["cooldown_seconds"],
        window_seconds=exploration["window_seconds"],
        initial_center_hz=exploration["initial_center_hz"], source=source)


def builtin_profile_names() -> list[str]:
    folder = resources.files("radiolack") / _BUILTIN_DIR
    return sorted(entry.name.removesuffix(".toml") for entry in folder.iterdir()
                  if entry.name.endswith(".toml"))


def load_profile(name_or_path: str) -> Profile:
    """Load a built-in profile by name, or a TOML profile file by path."""
    path = Path(name_or_path).expanduser()
    is_path = name_or_path.endswith(".toml") or "/" in name_or_path or path.exists()
    if is_path:
        source = str(path.absolute())
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as error:
            raise ProfileError(f"Cannot read profile {source}: {error.strerror or error}") from None
    else:
        if name_or_path not in builtin_profile_names():
            raise ProfileError(f"Unknown profile {name_or_path!r}; built-in profiles: "
                               f"{', '.join(builtin_profile_names())}")
        source = "built-in"
        resource = resources.files("radiolack") / _BUILTIN_DIR / f"{name_or_path}.toml"
        text = resource.read_text(encoding="utf-8")
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise ProfileError(f"Invalid TOML in profile {source}: {error}") from None
    return parse_profile(data, source)
