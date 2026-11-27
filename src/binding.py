"""Panel tapers, control and feedback maps, and the panel ↔ MOTU change
controllers (feeds.ChangeHandler).

MotuController writes panel input through the shared mixer.MixerService.
PanelController turns MOTU changes into panel feedback.
"""

from __future__ import annotations

import logging
import math
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import raw_panel

import mixer
import motu
from feeds import FEED_DATASTORE, FEED_METERS, FEED_RAW_PANEL

logger = logging.getLogger(__name__)

# The MixerService level scale for panel fader positions (FADER_TAPER).
FADER_SCALE = "fader"

OVERRIDE_PATH = "mix/monitor/0/override"
OVERRIDE_BASE = 1024.0
OVERRIDE_DISABLED = -1.0

# Ends of the panel fader and meter tapers, in dB.
MIN_DB = -120
MAX_DB = 12

# Panel raw ↔ Motu dB.
FADERS_RANGE_MAPPING = (
    (0, -math.inf),
    ((1, 5), (MIN_DB, -60)),
    ((5, 125), (-60, -30)),
    ((125, 1000), (-30, MAX_DB)),
)
METERS_RANGE_MAPPING = (
    (0, -math.inf),
    ((1, 1000), (-60, MAX_DB)),
)

_PATH_ROLE_RE = re.compile(r"(\w+)/?(\d*)$")


def _segments(mapping: tuple, *, reverse: bool = False) -> tuple:
    """Point (src, dst) or range ((lo, hi), (dst_lo, dst_hi)) → segments."""
    out = []
    for src, dst in mapping:
        if not isinstance(src, tuple):
            src, dst = (src, src), (dst, dst)
        if reverse:
            src, dst = dst, src
        out.append((float(src[0]), float(src[1]), float(dst[0]), float(dst[1])))
    return tuple(out)


def _lerp(value: float, segments: tuple) -> float:
    """Map value through segments. Clamps outside. Intervals are half-open."""
    first_lo, _, first_dst, _ = segments[0]
    _, last_hi, _, last_dst = segments[-1]
    if value <= first_lo:
        return first_dst
    if value >= last_hi:
        return last_dst
    for lo, hi, dst_lo, dst_hi in segments:
        if hi == lo:
            continue
        if lo <= value < hi:
            return dst_lo + (value - lo) / (hi - lo) * (dst_hi - dst_lo)
    return first_dst


class PiecewiseTaper:
    """A DbTaper interpolated linearly between the points and ranges of a table."""

    def __init__(self, mapping: tuple) -> None:
        self._to_db = _segments(mapping)
        self._from_db = _segments(mapping, reverse=True)

    def to_db(self, position: float) -> float:
        return _lerp(float(position), self._to_db)

    def from_db(self, db: float) -> int:
        return int(_lerp(float(db), self._from_db))


FADER_TAPER = PiecewiseTaper(FADERS_RANGE_MAPPING)
METER_TAPER = PiecewiseTaper(METERS_RANGE_MAPPING)
# MixerService level scales for the panel, by name, besides linear and db.
SCALES: Mapping[str, mixer.LevelScale] = MappingProxyType(
    {FADER_SCALE: mixer.TaperedScale(FADER_TAPER)}
)


def role_from_path(path: str) -> str:
    """Infer control role from the last Motu path segment."""
    m = _PATH_ROLE_RE.search(path)
    if not m:
        return "unknown"
    name = m.group(1)
    if name in ("send", "fader"):
        return "send"
    return name


def kind_from_name(name: str) -> str:
    """Infer feedback kind from the raw target key."""

    if name.startswith("button"):
        return "button"
    if name == "fader":
        return "fader"
    if name == "display":
        return "display"
    if name.startswith("display") and name.removeprefix("display").isdigit():
        return "meter"
    return "unknown"


@dataclass(frozen=True, slots=True)
class BindingConfig:
    """Resolved HWC → Motu binding. Optional fields are role-specific."""

    path: str
    role: str
    default_level_db: float | None = None
    override_index: int | None = None

    @property
    def override_value(self) -> float | None:
        """OVERRIDE_BASE + index, or None when the index is not set."""
        if self.override_index is None:
            return None
        return OVERRIDE_BASE + float(self.override_index)


@dataclass(frozen=True, slots=True)
class FeedbackTarget:
    """Normalized feedback target. Built once from the raw table."""

    # TODO: Infer kind from the panel topology
    kind: str
    hwcid: int
    mode: dict | None = None
    color: dict | None = None
    value: float | None = None
    text: dict | None = None
    channels: tuple[int, ...] | None = None
    pre_fader: bool = True
    # TODO: Infer fader_path and mute_path from channels
    fader_path: str | None = None
    mute_path: str | None = None
    audio_meter: dict | None = None


class _Map(Mapping):
    """Read-only key → value mapping. Subclasses only implement from_raw."""

    def __init__(self, bindings: Mapping) -> None:
        self._bindings = dict(bindings)

    def __getitem__(self, key: str):
        return self._bindings[key]

    def __iter__(self):
        return iter(self._bindings)

    def __len__(self) -> int:
        return len(self._bindings)


class ControlMap(_Map):
    """hwcid → BindingConfig (roles resolved once at load)."""

    @classmethod
    def from_raw(cls, raw: Mapping[str, Mapping[str, Any]]) -> ControlMap:
        out: dict[str, BindingConfig] = {}
        for hwcid, entry in raw.items():
            path = entry["path"]
            role = entry.get("role") or role_from_path(path)
            default = entry.get("default_level")
            override_index = entry.get("override_index")
            out[str(hwcid)] = BindingConfig(
                path=path,
                role=role,
                default_level_db=(float(default) if default is not None else None),
                override_index=(
                    int(override_index) if override_index is not None else None
                ),
            )
        return cls(out)


class FeedbackMap(_Map):
    """Motu path → tuple of feedback targets."""

    @classmethod
    def from_raw(cls, raw: Mapping[str, Mapping[str, Any]]) -> FeedbackMap:
        out: dict[str, tuple[FeedbackTarget, ...]] = {}
        for path, targets in raw.items():
            built: list[FeedbackTarget] = []
            for name, cfg in targets.items():
                channels = cfg.get("channels")
                built.append(
                    FeedbackTarget(
                        kind=kind_from_name(name),
                        hwcid=int(cfg["hwcid"]),
                        mode=cfg.get("mode"),
                        color=cfg.get("color"),
                        value=cfg.get("value"),
                        text=cfg.get("text"),
                        channels=tuple(channels) if channels is not None else None,
                        pre_fader=bool(cfg.get("pre_fader", True)),
                        fader_path=cfg.get("fader_path"),
                        mute_path=cfg.get("mute_path"),
                        audio_meter=cfg.get("audio_meter"),
                    )
                )
            out[path] = tuple(built)
        return cls(out)


class MotuController:
    """Panel HWC → MOTU mixer, through the shared MixerService."""

    def __init__(self, control_map: ControlMap, service: mixer.MixerService) -> None:
        self._map = control_map
        self._mixer = service
        self._role_handlers = {
            "send": self._handle_send,
            "mute": self._handle_mute,
            "solo": self._handle_solo,
            "override": self._handle_override,
        }

    def _read(self, path: str) -> Any | None:
        """The current value at path, or None (logged) if it can't be read."""
        try:
            return self._mixer.read(path).value
        except mixer.MixerError as e:
            logger.warning("Unable to read path %s: %s", path, e)
            return None

    async def _write(
        self,
        path: str,
        value: object,
        scale: str | None = None,
        *,
        origin: str | None = None,
    ) -> None:
        try:
            await self._mixer.write(path, value, scale, origin=origin)
        except mixer.MixerError as e:
            logger.warning("Unable to set path %s to %s: %s", path, value, e)

    async def _toggle(self, path: str) -> None:
        try:
            await self._mixer.toggle(path)
        except mixer.MixerError as e:
            logger.warning("Unable to toggle path %s: %s", path, e)

    async def on_change(
        self, data: dict, *, feed: str, origin: str | None = None
    ) -> None:
        if feed != FEED_RAW_PANEL:
            logger.debug("MotuController: ignore feed=%s", feed)
            return
        await self.process_hwc(data["hwcid"], data["value"])

    async def process_hwc(self, hwcid: str | int, value: str) -> None:
        """Apply one normalized HWC value ('Down' / 'Up' / fader level)."""
        # TODO: Prepare to handle 'Up' values as well.
        cfg = self._map.get(hwcid)
        if cfg is None:
            logger.debug("hwcid %s is not mapped", hwcid)
            return
        logger.debug("hwcid %s value=%r role=%s", hwcid, value, cfg.role)

        handler = self._role_handlers.get(cfg.role)
        if handler is None:
            logger.debug("Unhandled role %s for hwcid %s", cfg.role, hwcid)
            return
        await handler(cfg, value, hwcid)

    def _toggled_level(self, cfg: BindingConfig) -> float | None:
        """Next linear gain: toggles between the default level and unity."""
        current = self._read(cfg.path)
        if current is None:
            return None
        if cfg.default_level_db is None:
            logger.debug("No default level for path %s", cfg.path)
            return 1.0
        # Rounded like the MOTU stores it, so it compares equal after a poll.
        default = round(
            mixer.db_to_gain(min(cfg.default_level_db, MAX_DB)),
            mixer.LEVEL_DECIMALS,
        )
        return default if current != default else 1.0

    async def _handle_send(self, cfg: BindingConfig, v: str, hwcid: str | int) -> None:
        try:
            position = int(v)
        except ValueError:
            if v != "Down":
                return
            level = self._toggled_level(cfg)
            if level is not None:
                await self._write(cfg.path, level, "linear", origin=str(hwcid))
            return
        await self._write(cfg.path, position, FADER_SCALE, origin=str(hwcid))

    async def _handle_mute(self, cfg: BindingConfig, v: str, hwcid: str | int) -> None:
        if v != "Down":
            return
        await self._toggle(cfg.path)

    async def _handle_solo(self, cfg: BindingConfig, v: str, hwcid: str | int) -> None:
        if v != "Down":
            return
        override = self._read(OVERRIDE_PATH)
        if override is not None and override != OVERRIDE_DISABLED:
            await self._write(cfg.path, True)
        else:
            await self._toggle(cfg.path)
        await self._write(OVERRIDE_PATH, OVERRIDE_DISABLED)

    async def _handle_override(
        self, cfg: BindingConfig, v: str, hwcid: str | int
    ) -> None:
        if v != "Down":
            return
        if cfg.override_value is None:
            logger.warning("Missing override_index for %s", cfg.path)
            return
        current = self._read(cfg.path)
        if current is None:
            return
        if current == cfg.override_value:
            value = OVERRIDE_DISABLED
        else:
            value = cfg.override_value
        await self._write(cfg.path, value)


class PanelController:
    """Motu → panel feedback.

    Reads the datastore and meters, and sends feedback to the panel.

    Public: on_change, init_feedback
    Private: _process_data_feedback, _process_meters_feedback
    """

    def __init__(
        self,
        feedback_map: FeedbackMap,
        *,
        panel: raw_panel.RawPanel,
        datastore: mixer.Store,
        meters: motu.Meters,
        fader_taper: mixer.DbTaper,
        meter_taper: mixer.DbTaper,
    ) -> None:
        self._map = feedback_map
        self._panel = panel
        self._ds = datastore
        self._ms = meters
        self._fader_scale = mixer.TaperedScale(fader_taper)
        self._meter_scale = mixer.TaperedScale(meter_taper)
        self._meter_multipliers: dict[int, float] = {}
        self._silent_meters: set[int] = set()
        self._last_peaks = None
        self._kind_handlers = {
            "button": self._handle_button,
            "fader": self._handle_fader,
            "display": self._handle_display,
        }

    @property
    def _meter_targets(self) -> tuple[FeedbackTarget, ...]:
        return tuple(
            target
            for target in (self._map.get("mix/level") or ())
            if target.kind == "meter"
        )

    def _update_meter_multiplier_for_path(self, path: str) -> None:
        for target in self._meter_targets:
            if path not in (target.fader_path, target.mute_path):
                continue
            multiplier = self._calculate_meter_multiplier(target)
            if multiplier is not None:
                self._meter_multipliers[target.hwcid] = multiplier

    async def on_change(
        self, data: dict, *, feed: str, origin: str | None = None
    ) -> None:
        if feed == FEED_DATASTORE:
            await self._process_data_feedback(data, origin=origin)
        elif feed == FEED_METERS:
            await self._process_meters_feedback(data)
        else:
            logger.debug("PanelController: ignore feed=%s", feed)

    async def init_feedback(self) -> None:
        """Seed panel from current Motu snapshots."""
        logger.info("Initializing the panel feedback")
        dd = {}
        md = {}
        for path in self._map:
            v = self._ds.get(path)
            if v is None:
                for i in [str(s) for s in range(motu.METER_SLOTS)] + ["peaks"]:
                    m_path = f"{path}/{i}"
                    v = self._ms.get(m_path)
                    if v is None:
                        logger.warning("Path %s is not available", path)
                        continue
                    md[m_path] = v
            else:
                dd[path] = v
        logger.debug("Init datastore feedback %s", dd)
        await self._process_data_feedback(dd, init=True)
        logger.debug("Init meters feedback %s", md)
        # Force the controller to send data to all audio meters
        self._last_peaks = None
        await self._process_meters_feedback(md)

    @staticmethod
    def _is_status_dependent(style: dict | None) -> bool:
        """True when a field picks a value from the on/off state."""
        if not style:
            return False
        return any(
            isinstance(value, (tuple, list)) and len(value) == 2
            for value in style.values()
        )

    @staticmethod
    def _resolve_style(style: dict, active: bool) -> dict:
        """Resolve a mode or color dict. A pair is (on, off)."""
        resolved = {}
        for key, value in style.items():
            if isinstance(value, (tuple, list)) and len(value) == 2:
                resolved[key] = value[0] if active else value[1]
            else:
                resolved[key] = value
        return resolved

    def _handle_button(
        self,
        target: FeedbackTarget,
        value: Any,
        *,
        echo: bool = False,
        init: bool = False,
    ) -> dict:
        active = (value == target.value) if target.value is not None else bool(value)
        msg: dict = {}
        if target.mode and (init or self._is_status_dependent(target.mode)):
            msg.update(
                self._panel.hwc_mode(
                    target.hwcid, **self._resolve_style(target.mode, active)
                )
            )
        if target.color and (init or self._is_status_dependent(target.color)):
            msg.update(
                self._panel.hwc_color(
                    target.hwcid, **self._resolve_style(target.color, active)
                )
            )
        return msg

    def _handle_fader(
        self,
        target: FeedbackTarget,
        value: Any,
        *,
        echo: bool = False,
        init: bool = False,
    ) -> dict:
        msg: dict = {}
        if target.mode and (init or self._is_status_dependent(target.mode)):
            msg.update(
                self._panel.hwc_mode(
                    target.hwcid, **self._resolve_style(target.mode, True)
                )
            )
        if target.color and (init or self._is_status_dependent(target.color)):
            msg.update(
                self._panel.hwc_color(
                    target.hwcid, **self._resolve_style(target.color, True)
                )
            )
        if not echo:
            position = int(self._fader_scale.from_gain(float(value)))
            msg.update(self._panel.hwc_fader(target.hwcid, position))
        return msg

    def _handle_display(
        self,
        target: FeedbackTarget,
        value: Any,
        *,
        echo: bool = False,
        init: bool = False,
    ) -> dict:
        txt = target.text or {}
        return self._panel.hwc_text(
            target.hwcid,
            text1=f"{mixer.gain_to_db(value): 5.1f}",
            **txt,
        )

    async def _process_data_feedback(
        self, d: dict, *, origin: str | None = None, init: bool = False
    ) -> None:
        # TODO: Consider waking the panel
        # TODO: Only change display text during feedback init
        messages = []
        for path, value in d.items():
            mapping = self._map.get(path)
            if mapping is None:
                logger.debug("path %s is not mapped", path)
                continue
            if path.rsplit("/", 1)[-1] in ("mute", "fader", "send"):
                self._update_meter_multiplier_for_path(path)
            for target in mapping:
                handler = self._kind_handlers.get(target.kind)
                if handler is None:
                    logger.debug(
                        "Unhandled target kind %s for path %s",
                        target.kind,
                        path,
                    )
                    continue
                echo = origin is not None and str(target.hwcid) == origin
                msg = handler(target, value, echo=echo, init=init)
                if msg:
                    messages.append(msg)
        batched = self._panel.batch_hwc(messages)
        if batched is not None:
            await self._panel.send(batched)

    def _calculate_meter_multiplier(self, target: FeedbackTarget) -> float | None:
        if target.pre_fader:
            return 1.0
        fader = self._ds.get(target.fader_path)
        muted = self._ds.get(target.mute_path)
        if fader is None or muted is None:
            logger.warning("fader_path and mute_path required when pre_fader is False")
            return None
        return float(fader) * float(not muted)

    @staticmethod
    def _get_meter_sample(samples: list | None, index: int | None) -> float | None:
        try:
            return samples[index]
        except KeyError, IndexError, TypeError:
            return None

    def _get_channel_by_slot(self, channels: tuple[int, ...], slot: int) -> int | None:
        """Return the channels[slot], or None if missing."""
        try:
            return channels[slot]
        except IndexError:
            return None

    def _meter_level_to_raw(self, value: int, *, multiplier: float = 1.0) -> int:
        """Meter sample (0 to METER_SCALE) → panel raw."""
        return int(self._meter_scale.from_gain(value * multiplier / motu.METER_SCALE))

    @staticmethod
    def _split_meter_payload(
        data: dict,
    ) -> tuple[list | None, list | None]:
        """Return (level_samples, peak_samples) from a meters diff."""
        level_samples = None
        peak_samples = None
        for key, samples in data.items():
            if not key.startswith("mix/level"):
                continue
            suffix = key.rsplit("/", 1)[-1]
            if suffix == "peaks":
                peak_samples = samples
            else:
                level_samples = samples
        return level_samples, peak_samples

    def _get_meter_multiplier(self, target: FeedbackTarget) -> float:
        try:
            return self._meter_multipliers[target.hwcid]
        except KeyError:
            multiplier = self._calculate_meter_multiplier(target)
            if multiplier is None:
                return 1.0
            self._meter_multipliers[target.hwcid] = multiplier
            return multiplier

    def _is_channel_changed(
        self,
        ch: int,
        levels: list | None,
        prev_levels: list | None,
        peaks: list | None,
        prev_peaks: list | None,
    ) -> bool:
        """True when the channel level or peak differs.

        No previous value means changed.
        """
        if prev_levels is None or self._get_meter_sample(
            levels, ch
        ) != self._get_meter_sample(prev_levels, ch):
            return True
        if peaks is None or prev_peaks is None:
            return peaks is not None
        return self._get_meter_sample(peaks, ch) != self._get_meter_sample(
            prev_peaks, ch
        )

    def _bundle_meter_samples(
        self,
        level_samples: list | None,
        peak_samples: list | None,
        channels: tuple[int, ...],
        multiplier: float,
    ) -> list:
        bundle = []
        for samples in (level_samples, peak_samples):
            for slot in (0, 1):
                channel = self._get_channel_by_slot(channels, slot)
                sample = self._get_meter_sample(samples, channel)
                bundle.append(
                    None
                    if sample is None
                    else self._meter_level_to_raw(sample, multiplier=multiplier)
                )
        return bundle

    async def _process_meters_feedback(self, d: dict) -> None:
        # TODO: Only send audio_meter during feedback init
        self._panel.last_activity = time.perf_counter()
        displays = self._meter_targets
        if not displays:
            logger.warning("path mix/level is not mapped")
            return

        level_samples, peak_samples = self._split_meter_payload(d)
        if level_samples is None:
            return
        prev_levels = self._ms.previous_levels
        prev_peaks = self._last_peaks
        await self._panel.wakeup()
        messages = []
        for target in displays:
            audio_meter = target.audio_meter
            if audio_meter is None:
                continue
            channels = target.channels or ()
            multiplier = self._get_meter_multiplier(target)
            if multiplier == 0.0 and prev_peaks is not None:
                if target.hwcid in self._silent_meters:
                    continue
                self._silent_meters.add(target.hwcid)
            else:
                self._silent_meters.discard(target.hwcid)
                if prev_levels is not None and not any(
                    self._is_channel_changed(
                        ch,
                        level_samples,
                        prev_levels,
                        peak_samples,
                        prev_peaks,
                    )
                    for ch in channels
                ):
                    continue
            bundle = self._bundle_meter_samples(
                level_samples,
                peak_samples,
                channels,
                multiplier,
            )
            messages.append(
                self._panel.hwc_meter(
                    target.hwcid,
                    data1=bundle[0],
                    data2=bundle[1],
                    peak1=bundle[2],
                    peak2=bundle[3],
                    **audio_meter,
                )
            )
        batched = self._panel.batch_hwc(messages)
        if batched is not None:
            await self._panel.send(batched)
        if peak_samples is not None:
            self._last_peaks = peak_samples
