"""MOTU mixer controls, described by the device's own mix/ctrls specs.

Every value under mix/ in the datastore resolves to a Control. Its codec,
built from the matching mix/ctrls spec, validates and converts values between
clients (the REST API and the panel) and the datastore. Values without a spec
are read-only.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Protocol

# A read-only empty mapping, safe to share as a default.
EMPTY: Mapping[str, Any] = MappingProxyType({})

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class MixerError(Exception):
    """Base class for errors raised by MixerService."""


class NotLoaded(MixerError):
    pass


class NotFound(MixerError):
    pass


class ReadOnly(MixerError):
    pass


class InvalidValue(MixerError):
    pass


class NotToggleable(MixerError):
    pass


class Rejected(MixerError):
    pass


# ---------------------------------------------------------------------------
# Levels
# ---------------------------------------------------------------------------

# The MOTU's level resolution: it stores anything below 5e-7 as 0.
LEVEL_DECIMALS = 6


def gain_to_db(gain: int | float) -> float:
    """Linear gain to dB. 0 returns -inf."""
    gain = float(gain)
    if gain == 0:
        return -math.inf
    return 20 * math.log10(gain)


def db_to_gain(db: int | float) -> float:
    """dB to linear gain. -inf returns 0, beyond the float range inf."""
    try:
        return 10 ** (float(db) / 20)
    except OverflowError:
        return math.inf


# ---------------------------------------------------------------------------
# Codecs
# ---------------------------------------------------------------------------


def _number(value: object) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not math.isfinite(value)
    ):
        raise InvalidValue("'value' must be a finite number")
    return float(value)


@dataclass(frozen=True, slots=True)
class Reading:
    """A decoded value, with its scale (levels) or unit (numbers)."""

    value: Any
    meta: Mapping[str, Any] = field(default_factory=lambda: EMPTY)


class LevelScale(Protocol):
    """A way to write a level as a number.

    Converts between that number and the MOTU's linear gain.
    """

    def to_gain(self, number: object) -> float:
        """Number -> gain; raises InvalidValue for an invalid number."""
        ...

    def from_gain(self, gain: float) -> float:
        """Gain -> number; -inf for silence where the scale has no zero."""
        ...


class DbTaper(Protocol):
    """Maps positions on a taper, such as the panel's fader or meter, to dB and back."""

    def to_db(self, position: float) -> float: ...

    def from_db(self, db: float) -> float: ...


@dataclass(frozen=True, slots=True)
class LinearScale:
    """The number is the linear gain, the MOTU's native value."""

    def to_gain(self, number: object) -> float:
        """Finite number -> gain; None is rejected."""
        return _number(number)

    def from_gain(self, gain: float) -> float:
        """Gain -> the same number."""
        return gain


@dataclass(frozen=True, slots=True)
class DecibelScale:
    """The number is in dB; -inf is silence."""

    def to_gain(self, number: object) -> float:
        """dB -> gain; -inf is silence."""
        return 0.0 if number == -math.inf else db_to_gain(_number(number))

    def from_gain(self, gain: float) -> float:
        """Gain -> dB; silence is -inf."""
        return gain_to_db(gain)


@dataclass(frozen=True, slots=True)
class TaperedScale:
    """The number is a position on a DbTaper, such as the panel's fader."""

    taper: DbTaper

    def to_gain(self, number: object) -> float:
        """Taper position -> gain, via dB."""
        return db_to_gain(self.taper.to_db(_number(number)))

    def from_gain(self, gain: float) -> float:
        """Gain -> taper position, via dB."""
        return self.taper.from_db(gain_to_db(gain))


LINEAR = LinearScale()
DB = DecibelScale()
# Built-in level scales, read-only as all level codecs share them; the first is
# the default.
LEVEL_SCALES: Mapping[str, LevelScale] = MappingProxyType({"linear": LINEAR, "db": DB})


class Codec(Protocol):
    def decode(self, stored: Any, scale: str | None) -> Reading: ...

    def encode(self, value: object, scale: str | None) -> float: ...


@dataclass(frozen=True, slots=True)
class BoolCodec:
    def decode(self, stored: Any, scale: str | None) -> Reading:
        return Reading(bool(stored))

    def encode(self, value: object, scale: str | None) -> float:
        if not isinstance(value, bool):
            raise InvalidValue("'value' must be true or false")
        return float(value)


@dataclass(frozen=True, slots=True)
class NumberCodec:
    minimum: float
    maximum: float
    unit: str | None = None

    def decode(self, stored: Any, scale: str | None) -> Reading:
        return Reading(float(stored), {"unit": self.unit})

    def encode(self, value: object, scale: str | None) -> float:
        number = _number(value)
        if not self.minimum <= number <= self.maximum:
            raise InvalidValue(
                f"'value' must be between {self.minimum:g} and {self.maximum:g}"
            )
        return number


@dataclass(frozen=True, slots=True)
class EnumCodec:
    options: Mapping[int, str]

    def decode(self, stored: Any, scale: str | None) -> Reading:
        return Reading(int(stored))

    def encode(self, value: object, scale: str | None) -> float:
        if isinstance(value, bool) or value not in self.options:
            choices = ", ".join(f"{k} ({v})" for k, v in self.options.items())
            raise InvalidValue(f"'value' must be one of: {choices}")
        return float(value)


@dataclass(frozen=True, slots=True)
class LevelCodec:
    """Linear gain within [minimum, maximum], written on any of its scales.

    Written gains are limited to the range and rounded to the MOTU's
    resolution.
    """

    minimum: float
    maximum: float
    scales: Mapping[str, LevelScale] = field(default_factory=lambda: LEVEL_SCALES)

    def decode(self, stored: Any, scale: str | None) -> Reading:
        return Reading(self.scales[scale].from_gain(float(stored)), {"scale": scale})

    def encode(self, value: object, scale: str | None) -> float:
        gain = self.scales[scale].to_gain(value)
        return round(min(max(gain, self.minimum), self.maximum), LEVEL_DECIMALS)


def parse_spec(spec: str, scales: Mapping[str, LevelScale] = LEVEL_SCALES) -> Codec:
    """Codec for a mix/ctrls spec, with the scales levels can use.

    Specs look like 'bool:0', 'enum:0:0=RMS:1=Peak', 'real:2:1:10',
    'int:100:20:20000:Hz' and 'real:0:0:4:linear' (default first).
    """
    kind, _default, *rest = spec.split(":")
    if kind == "bool":
        return BoolCodec()
    if kind == "enum":
        return EnumCodec({int(k): v for k, v in (o.split("=", 1) for o in rest)})
    minimum, maximum, *unit = rest
    if unit == ["linear"]:
        return LevelCodec(float(minimum), float(maximum), scales)
    return NumberCodec(float(minimum), float(maximum), unit[0] if unit else None)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

# Datastore path -> mix/ctrls key, first match wins. Sends and pans towards
# another bus share the matrix fader and pan specs.
CTRL_RULES = (
    (re.compile(r"^mix/talkback/(\w+)$"), r"talkback/\1"),
    (re.compile(r"^mix/\w+/\d+/matrix/\w+/\d+/send$"), "matrix/fader"),
    (re.compile(r"^mix/\w+/\d+/matrix/\w+/\d+/pan$"), "matrix/pan"),
    (re.compile(r"^mix/\w+/\d+/(.+)$"), r"\1"),
)


class Store(Protocol):
    """The datastore operations Controls and MixerService need (motu.DataStore)."""

    data: dict

    def get(self, path: str) -> Any | None: ...

    async def set(self, path: str, value: Any) -> Any | None: ...

    async def toggle(self, path: str) -> float | None: ...


@dataclass(frozen=True, slots=True)
class Control:
    path: str
    codec: Codec | None  # None: read-only


class Controls:
    """Datastore path -> Control, using the specs found in the datastore."""

    def __init__(
        self, store: Store, *, scales: Mapping[str, LevelScale] | None = None
    ) -> None:
        """scales: extra level scales by name, besides linear and db."""
        self._store = store
        self._scales = MappingProxyType({**LEVEL_SCALES, **(scales or {})})
        self._codecs: dict[str, Codec | None] = {}

    def _ctrl_key(self, path: str) -> str | None:
        for pattern, key in CTRL_RULES:
            if pattern.match(path):
                return pattern.sub(key, path)
        return None

    def _codec(self, path: str) -> Codec | None:
        key = self._ctrl_key(path)
        if key is None:
            return None
        # mix/ctrls/<strip> lists the sections a strip type supports.
        sections = self._store.get(f"mix/ctrls/{path.split('/')[1]}")
        if isinstance(sections, str) and key.split("/")[0] not in sections.split(":"):
            return None
        spec = self._store.get(f"mix/ctrls/{key}")
        return parse_spec(spec, self._scales) if isinstance(spec, str) else None

    def get(self, path: str) -> Control | None:
        """The control at path, or None if the datastore has no such value."""
        if not path.startswith("mix/") or path not in self._store.data:
            return None
        if path not in self._codecs:
            self._codecs[path] = self._codec(path)
        return Control(path, self._codecs[path])


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class MixerService:
    """Reads and writes mixer values with validation and level scale conversion."""

    def __init__(
        self, store: Store, *, scales: Mapping[str, LevelScale] | None = None
    ) -> None:
        """scales: extra level scales by name, besides linear and db."""
        self._store = store
        self._controls = Controls(store, scales=scales)

    def _control(self, path: str) -> Control:
        if not self._store.data:
            raise NotLoaded("MOTU data has not been loaded yet")
        control = self._controls.get(path)
        if control is None:
            raise NotFound(f"No such MOTU path: {path}")
        return control

    @staticmethod
    def _codec(control: Control) -> Codec:
        if control.codec is None:
            raise ReadOnly(f"{control.path} is read-only")
        return control.codec

    @staticmethod
    def _scale(control: Control, scale: str | None) -> str | None:
        """The scale to use for control: the given one, else the default."""
        codec = control.codec
        if not isinstance(codec, LevelCodec):
            if scale is not None:
                raise InvalidValue(f"{control.path} has no 'scale' option")
            return None
        if scale is None:
            return next(iter(codec.scales))
        if scale not in codec.scales:
            raise InvalidValue(f"'scale' must be one of: {', '.join(codec.scales)}")
        return scale

    def _reading(self, control: Control, scale: str | None) -> Reading:
        stored = self._store.get(control.path)
        if control.codec is None:
            return Reading(stored)
        return control.codec.decode(stored, scale)

    def read(self, path: str, scale: str | None = None) -> Reading:
        control = self._control(path)
        return self._reading(control, self._scale(control, scale))

    async def write(
        self,
        path: str,
        value: object,
        scale: str | None = None,
        response_scale: str | None = None,
    ) -> Reading:
        """scale is that of value; the response uses response_scale, else scale."""
        control = self._control(path)
        codec = self._codec(control)
        scale = self._scale(control, scale)
        response_scale = (
            scale if response_scale is None else self._scale(control, response_scale)
        )
        if await self._store.set(path, codec.encode(value, scale)) is None:
            raise Rejected(f"MOTU did not accept the change to {path}")
        return self._reading(control, response_scale)

    async def toggle(self, path: str) -> Reading:
        control = self._control(path)
        if not isinstance(self._codec(control), BoolCodec):
            raise NotToggleable(f"{path} is not an on/off control")
        if await self._store.toggle(path) is None:
            raise Rejected(f"MOTU did not accept the change to {path}")
        return self._reading(control, None)
