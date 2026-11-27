"""Mixer levels, scales, codecs, controls and service."""

import inspect
import math
import re

import pytest
from pytest import param

import binding
import mixer
from motu import DEVICE_DECIMALS

# FADER_TAPER puts silence at 0, -30 dB at 125, 0 dB at 750 and +12 dB at 1000.
FADER = mixer.TaperedScale(binding.FADER_TAPER)
# A send's codec as api.py's service builds it, with the panel's scales.
SEND = mixer.LevelCodec(0.0, 4.0, {**mixer.LEVEL_SCALES, **binding.SCALES})
PEAK = mixer.EnumCodec({0: "RMS", 1: "Peak"})
EQ_GAIN = mixer.NumberCodec(-20.0, 20.0, "dB")
PLUS_12_DB = round(10**0.6, DEVICE_DECIMALS)
MINUS_10_DB = round(10**-0.5, DEVICE_DECIMALS)

MUTE_PATH = "mix/chan/0/matrix/mute"
SEND_PATH = "mix/chan/0/matrix/aux/0/send"
NO_SUCH_PATH = "No such MOTU path: mix/chan/48/matrix/mute"
SCALES = "'scale' must be one of: linear, db, fader"

# ---------------------------------------------------------------------------
# Levels
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("gain", "db"),
    [
        param(1, 0.0, id="unity"),
        param(10, 20.0, id="plus-20"),
        param(0, -math.inf, id="silence"),
    ],
)
def test_gain_to_db(gain, db):
    assert mixer.gain_to_db(gain) == db


@pytest.mark.parametrize(
    ("db", "gain"),
    [
        param(0, 1.0, id="unity"),
        param(20, 10.0, id="plus-20"),
        param(-math.inf, 0.0, id="silence"),
        param(1e300, math.inf, id="overflow"),
    ],
)
def test_db_to_gain(db, gain):
    assert mixer.db_to_gain(db) == gain


# ---------------------------------------------------------------------------
# Scales
# ---------------------------------------------------------------------------


def test_level_scales_default_first():
    assert dict(mixer.LEVEL_SCALES) == {"linear": mixer.LINEAR, "db": mixer.DB}
    assert next(iter(mixer.LEVEL_SCALES)) == "linear"


@pytest.mark.parametrize(
    ("scale", "number", "gain"),
    [
        param(mixer.LINEAR, 0.123456789, 0.123456789, id="linear"),
        param(mixer.DB, 0, 1.0, id="db-unity"),
        param(mixer.DB, 20, 10.0, id="db-plus-20"),
        param(mixer.DB, -math.inf, 0.0, id="db-silence"),
        param(FADER, 0, 0.0, id="fader-bottom"),
        param(FADER, 1000, 10**0.6, id="fader-top"),
    ],
)
def test_scale_to_gain(scale, number, gain):
    assert scale.to_gain(number) == gain


@pytest.mark.parametrize(
    ("scale", "gain", "number"),
    [
        param(mixer.LINEAR, 0.3, 0.3, id="linear"),
        param(mixer.DB, 1.0, 0.0, id="db-unity"),
        param(mixer.DB, 0.0, -math.inf, id="db-silence"),
        param(FADER, 1.0, 750, id="fader-unity"),
        param(FADER, 0.0, 0, id="fader-silence"),
        param(FADER, PLUS_12_DB, 1000, id="fader-top"),
    ],
)
def test_scale_from_gain(scale, gain, number):
    assert scale.from_gain(gain) == number


INVALID_NUMBERS = {
    "null": None,
    "bool": True,
    "string": "1",
    "list": [1],
    "inf": math.inf,
    "nan": math.nan,
}


@pytest.mark.parametrize(
    ("scale", "number"),
    [
        *(
            param(scale, number, id=f"{name}-{kind}")
            for name, scale in (
                ("linear", mixer.LINEAR),
                ("db", mixer.DB),
                ("fader", FADER),
            )
            for kind, number in INVALID_NUMBERS.items()
        ),
        param(mixer.LINEAR, -math.inf, id="linear-minus-inf"),
        param(FADER, -math.inf, id="fader-minus-inf"),
    ],
)
def test_scale_rejects_invalid_number(scale, number):
    with pytest.raises(mixer.InvalidValue, match="'value' must be a finite number"):
        scale.to_gain(number)


# ---------------------------------------------------------------------------
# Codecs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("spec", "scales", "codec"),
    [
        param("bool:0", mixer.LEVEL_SCALES, mixer.BoolCodec(), id="bool"),
        param("enum:0:0=RMS:1=Peak", mixer.LEVEL_SCALES, PEAK, id="enum"),
        param(
            "real:2:1:10", mixer.LEVEL_SCALES, mixer.NumberCodec(1.0, 10.0), id="number"
        ),
        param("real:0:-20:20:dB", mixer.LEVEL_SCALES, EQ_GAIN, id="number-unit"),
        param(
            "real:0:0:4:linear",
            mixer.LEVEL_SCALES,
            mixer.LevelCodec(0.0, 4.0),
            id="level",
        ),
        param("real:0:0:4:linear", SEND.scales, SEND, id="level-extra-scales"),
    ],
)
def test_parse_spec(spec, scales, codec):
    assert mixer.parse_spec(spec, scales) == codec


def test_level_codec_shares_level_scales():
    assert mixer.LevelCodec(0.0, 4.0).scales is mixer.LEVEL_SCALES


@pytest.mark.parametrize(
    ("codec", "stored", "scale", "reading"),
    [
        param(mixer.BoolCodec(), 1.0, None, mixer.Reading(True), id="bool-on"),
        param(mixer.BoolCodec(), 0, None, mixer.Reading(False), id="bool-off"),
        param(PEAK, 1.0, None, mixer.Reading(1), id="enum"),
        param(EQ_GAIN, 3, None, mixer.Reading(3.0, {"unit": "dB"}), id="number"),
        param(
            mixer.NumberCodec(0.0, 1.0),
            1,
            None,
            mixer.Reading(1.0, {"unit": None}),
            id="number-no-unit",
        ),
        param(
            mixer.NumberCodec(20.0, 20000.0, "Hz"),
            495.51001,
            None,
            mixer.Reading(495.51001, {"unit": "Hz"}),
            id="number-not-rounded",
        ),
        param(
            SEND, 0.5, "linear", mixer.Reading(0.5, {"scale": "linear"}), id="linear"
        ),
        param(SEND, 0, "linear", mixer.Reading(0.0, {"scale": "linear"}), id="int"),
        param(SEND, 1.0, "db", mixer.Reading(0.0, {"scale": "db"}), id="db"),
        param(
            SEND,
            0.5,
            "db",
            mixer.Reading(20 * math.log10(0.5), {"scale": "db"}),
            id="db-not-rounded",
        ),
        param(
            SEND, 0.0, "db", mixer.Reading(-math.inf, {"scale": "db"}), id="db-silence"
        ),
        param(SEND, 0.0, "fader", mixer.Reading(0, {"scale": "fader"}), id="fader"),
    ],
)
def test_codec_decode(codec, stored, scale, reading):
    assert codec.decode(stored, scale) == reading


@pytest.mark.parametrize(
    ("codec", "value", "scale", "stored"),
    [
        param(mixer.BoolCodec(), True, None, 1.0, id="bool-on"),
        param(mixer.BoolCodec(), False, None, 0.0, id="bool-off"),
        param(PEAK, 1, None, 1.0, id="enum"),
        param(EQ_GAIN, 20, None, 20.0, id="number-max"),
        param(EQ_GAIN, -0.14, None, -0.14, id="number"),
        param(SEND, 0.25, "linear", 0.25, id="linear"),
        param(SEND, 0.1234567, "linear", 0.123457, id="linear-rounds"),
        param(SEND, 4e-7, "linear", 0.0, id="linear-below-resolution"),
        param(SEND, 6e-7, "linear", 1e-6, id="linear-resolution"),
        param(SEND, 100, "linear", 4.0, id="linear-clamps-high"),
        param(SEND, -3, "linear", 0.0, id="linear-clamps-low"),
        param(SEND, -10, "db", MINUS_10_DB, id="db"),
        param(SEND, 12, "db", PLUS_12_DB, id="db-plus-12"),
        param(SEND, 13, "db", 4.0, id="db-clamps-high"),
        param(SEND, 1e300, "db", 4.0, id="db-overflow"),
        param(SEND, -120, "db", 1e-6, id="db-minus-120"),
        param(SEND, -121, "db", 1e-6, id="db-rounds-up"),
        param(SEND, -127, "db", 0.0, id="db-rounds-down"),
        param(SEND, -math.inf, "db", 0.0, id="db-silence"),
        param(SEND, 0, "fader", 0.0, id="fader-bottom"),
        param(SEND, 1000, "fader", PLUS_12_DB, id="fader-top"),
        param(SEND, 5000, "fader", PLUS_12_DB, id="fader-clamps-high"),
        param(SEND, 1e300, "fader", PLUS_12_DB, id="fader-overflow"),
        param(SEND, -50, "fader", 0.0, id="fader-clamps-low"),
        param(mixer.LevelCodec(0.5, 4.0), 0, "linear", 0.5, id="spec-minimum"),
        param(mixer.LevelCodec(0.5, 4.0), -200, "db", 0.5, id="spec-minimum-db"),
    ],
)
def test_codec_encode(codec, value, scale, stored):
    assert codec.encode(value, scale) == stored


@pytest.mark.parametrize(
    ("codec", "value", "scale", "message"),
    [
        param(mixer.BoolCodec(), 1, None, "must be true or false", id="bool-int"),
        param(mixer.BoolCodec(), "true", None, "must be true or false", id="bool-str"),
        param(mixer.BoolCodec(), None, None, "must be true or false", id="bool-null"),
        param(PEAK, 2, None, "one of: 0 (RMS), 1 (Peak)", id="enum-unknown"),
        param(PEAK, True, None, "one of: 0 (RMS), 1 (Peak)", id="enum-bool"),
        param(PEAK, "Peak", None, "one of: 0 (RMS), 1 (Peak)", id="enum-name"),
        param(PEAK, [], None, "one of: 0 (RMS), 1 (Peak)", id="enum-list"),
        param(PEAK, {}, None, "one of: 0 (RMS), 1 (Peak)", id="enum-dict"),
        param(EQ_GAIN, 20.5, None, "between -20 and 20", id="number-range"),
        param(EQ_GAIN, True, None, "finite number", id="number-bool"),
        param(EQ_GAIN, None, None, "finite number", id="number-null"),
        param(SEND, None, "linear", "finite number", id="level-null"),
    ],
)
def test_codec_encode_rejects(codec, value, scale, message):
    with pytest.raises(mixer.InvalidValue, match=re.escape(message)):
        codec.encode(value, scale)


# ---------------------------------------------------------------------------
# Controls
# ---------------------------------------------------------------------------


GATE = mixer.LevelCodec(0.0, 1.0, SEND.scales)


@pytest.mark.parametrize(
    ("path", "codec"),
    [
        param(MUTE_PATH, mixer.BoolCodec(), id="bool"),
        param(SEND_PATH, SEND, id="aux-send"),
        param(
            "mix/chan/0/matrix/group/2/send",
            SEND,
            id="group-send",
        ),
        param(
            "mix/chan/0/matrix/aux/2/pan",
            mixer.NumberCodec(-1.0, 1.0, "pan"),
            id="aux-pan",
        ),
        param("mix/chan/0/gate/threshold", GATE, id="threshold"),
        param("mix/talkback/dim", GATE, id="talkback-dim"),
        param("mix/chan/0/comp/peak", PEAK, id="enum"),
        param(
            "mix/chan/0/eq/highshelf/freq",
            mixer.NumberCodec(20.0, 20000.0, "Hz"),
            id="number",
        ),
        param("mix/aux/0/matrix/fader", SEND, id="aux-fader"),
        param(
            "mix/aux/0/eq/lowshelf/mode",
            mixer.EnumCodec({0: "Shelf", 1: "Para"}),
            id="aux-eq",
        ),
        param(
            "mix/group/2/leveler/makeup",
            mixer.NumberCodec(0.0, 100.0, "%"),
            id="group-leveler",
        ),
        param(
            "mix/reverb/0/reverb/reverbtime",
            mixer.NumberCodec(100.0, 60000.0, "ms"),
            id="reverb",
        ),
        param("mix/monitor/0/assign", mixer.NumberCodec(-2.0, 4096.0), id="monitor"),
        param("mix/talkback/chan", mixer.NumberCodec(-1.0, 48.0), id="talkback-chan"),
        param("mix/chan/0/config/format", None, id="no-spec"),
        param("mix/monitor/0/solocount", None, id="section-missing"),
        param("mix/meterStripOrder", None, id="no-strip"),
        param("mix/ctrls/matrix/mute", None, id="spec-itself"),
    ],
)
def test_controls_get(store, path, codec):
    """Controls as api.py builds them, with the panel's fader scale. A codec of
    None is a read-only control."""
    control = mixer.Controls(store, scales=binding.SCALES).get(path)
    assert control == mixer.Control(path, codec)


@pytest.mark.parametrize(
    "path",
    [
        param("mix/chan/48/matrix/mute", id="not-in-store"),
        param("mix/chan/0", id="strip"),
        param("chan/0/matrix/mute", id="no-mix-prefix"),
    ],
)
def test_controls_get_unknown(store, path):
    assert mixer.Controls(store).get(path) is None


def test_controls_get_caches_codecs_and_follows_the_store(store):
    """A spec is parsed once, so a later change to it is not picked up, but a
    value removed from the store is."""
    controls = mixer.Controls(store)
    controls.get(MUTE_PATH)
    store.data["mix/ctrls/matrix/mute"] = "real:0:0:1"
    assert controls.get(MUTE_PATH).codec == mixer.BoolCodec()
    del store.data[MUTE_PATH]
    assert controls.get(MUTE_PATH) is None


# ---------------------------------------------------------------------------
# MixerService
# ---------------------------------------------------------------------------


@pytest.fixture
def service(store):
    return mixer.MixerService(store, scales=binding.SCALES)


async def _call(service, operation: str, *args):
    """Run read (sync), write or toggle (async) by name."""
    result = getattr(service, operation)(*args)
    return await result if inspect.isawaitable(result) else result


@pytest.mark.parametrize(
    ("path", "scale", "reading"),
    [
        param("mix/chan/1/matrix/mute", None, mixer.Reading(True), id="bool"),
        param("mix/chan/0/comp/peak", None, mixer.Reading(0), id="enum"),
        param(
            "mix/chan/0/eq/mid1/gain",
            None,
            mixer.Reading(-0.14, {"unit": "dB"}),
            id="number",
        ),
        param(
            "mix/chan/0/matrix/aux/2/send",
            None,
            mixer.Reading(0.5, {"scale": "linear"}),
            id="level-default",
        ),
        param(SEND_PATH, "fader", mixer.Reading(750, {"scale": "fader"}), id="fader"),
        param("mix/chan/0/config/format", None, mixer.Reading("1:0"), id="read-only"),
    ],
)
def test_service_read(service, path, scale, reading):
    assert service.read(path, scale) == reading


@pytest.mark.parametrize(
    ("path", "value", "scale", "response_scale", "stored", "reading"),
    [
        param(MUTE_PATH, True, None, None, 1.0, mixer.Reading(True), id="bool"),
        param("mix/chan/0/comp/peak", 1, None, None, 1.0, mixer.Reading(1), id="enum"),
        param(
            "mix/chan/0/eq/mid1/gain",
            3.5,
            None,
            None,
            3.5,
            mixer.Reading(3.5, {"unit": "dB"}),
            id="number",
        ),
        param(
            SEND_PATH,
            -10,
            "db",
            None,
            MINUS_10_DB,
            mixer.Reading(20 * math.log10(MINUS_10_DB), {"scale": "db"}),
            id="db",
        ),
        param(
            SEND_PATH,
            12,
            "db",
            "fader",
            PLUS_12_DB,
            mixer.Reading(1000, {"scale": "fader"}),
            id="db-to-fader",
        ),
        param(
            SEND_PATH,
            1.0,
            None,
            "db",
            1.0,
            mixer.Reading(0.0, {"scale": "db"}),
            id="linear-to-db",
        ),
    ],
)
async def test_service_write(
    store, service, path, value, scale, response_scale, stored, reading
):
    assert await service.write(path, value, scale, response_scale) == reading
    assert store.writes == [(path, stored)]


@pytest.mark.parametrize(
    ("path", "reading", "stored"),
    [
        param(MUTE_PATH, mixer.Reading(True), 1.0, id="on"),
        param("mix/chan/1/matrix/mute", mixer.Reading(False), 0.0, id="off"),
        param("mix/talkback/enable", mixer.Reading(True), 1.0, id="stored-as-int"),
    ],
)
async def test_service_toggle(store, service, path, reading, stored):
    assert await service.toggle(path) == reading
    assert store.writes == [(path, stored)]


@pytest.mark.parametrize(
    ("operation", "args", "kwargs", "origin"),
    [
        param("write", (SEND_PATH, 750, "fader"), {"origin": "13"}, "13", id="write"),
        param("toggle", (MUTE_PATH,), {"origin": "1"}, "1", id="toggle"),
        param("write", (MUTE_PATH, False), {}, None, id="write-without-origin"),
    ],
)
async def test_service_forwards_origin(store, service, operation, args, kwargs, origin):
    """write and toggle pass origin to the store, for echo suppression."""
    await getattr(service, operation)(*args, **kwargs)
    assert store.origins == [origin]


@pytest.mark.parametrize(
    ("operation", "args", "error", "message"),
    [
        param(
            "read",
            ("mix/chan/48/matrix/mute",),
            mixer.NotFound,
            NO_SUCH_PATH,
            id="read-unknown",
        ),
        param(
            "read",
            (MUTE_PATH, "db"),
            mixer.InvalidValue,
            "has no 'scale' option",
            id="read-bool-scale",
        ),
        param(
            "read",
            ("mix/chan/0/comp/peak", "db"),
            mixer.InvalidValue,
            "has no 'scale' option",
            id="read-enum-scale",
        ),
        param(
            "read",
            ("mix/chan/0/config/format", "db"),
            mixer.InvalidValue,
            "has no 'scale' option",
            id="read-read-only-scale",
        ),
        param("read", (SEND_PATH, ""), mixer.InvalidValue, SCALES, id="read-empty"),
        param("read", (SEND_PATH, "DB"), mixer.InvalidValue, SCALES, id="read-case"),
        param("read", (SEND_PATH, 1), mixer.InvalidValue, SCALES, id="read-int"),
        param("read", (SEND_PATH, True), mixer.InvalidValue, SCALES, id="read-bool"),
        param("read", (SEND_PATH, []), mixer.InvalidValue, SCALES, id="read-list"),
        param("read", (SEND_PATH, {}), mixer.InvalidValue, SCALES, id="read-dict"),
        param(
            "write",
            (SEND_PATH, 0.5, []),
            mixer.InvalidValue,
            SCALES,
            id="write-list-scale",
        ),
        param(
            "write",
            ("mix/chan/48/matrix/mute", True),
            mixer.NotFound,
            NO_SUCH_PATH,
            id="write-unknown",
        ),
        param(
            "write",
            ("mix/chan/0/config/format", "2:0"),
            mixer.ReadOnly,
            "mix/chan/0/config/format is read-only",
            id="write-read-only",
        ),
        param(
            "write",
            (SEND_PATH, 0.5, None, "volts"),
            mixer.InvalidValue,
            SCALES,
            id="write-response-scale",
        ),
        param(
            "toggle",
            (SEND_PATH,),
            mixer.NotToggleable,
            "is not an on/off control",
            id="toggle-level",
        ),
        param(
            "toggle",
            ("mix/chan/0/comp/peak",),
            mixer.NotToggleable,
            "is not an on/off control",
            id="toggle-enum",
        ),
        param(
            "toggle",
            ("mix/chan/0/config/format",),
            mixer.ReadOnly,
            "is read-only",
            id="toggle-read-only",
        ),
        param(
            "toggle",
            ("mix/chan/48/matrix/mute",),
            mixer.NotFound,
            NO_SUCH_PATH,
            id="toggle-unknown",
        ),
    ],
)
async def test_service_errors(store, service, operation, args, error, message):
    with pytest.raises(error, match=re.escape(message)):
        await _call(service, operation, *args)
    assert not store.writes


@pytest.mark.parametrize(
    ("operation", "args"),
    [
        param("read", (MUTE_PATH,), id="read"),
        param("write", (MUTE_PATH, True), id="write"),
        param("toggle", (MUTE_PATH,), id="toggle"),
    ],
)
async def test_service_not_loaded(store, service, operation, args):
    store.data.clear()
    with pytest.raises(mixer.NotLoaded, match="has not been loaded"):
        await _call(service, operation, *args)


@pytest.mark.parametrize(
    ("operation", "args"),
    [
        param("write", (MUTE_PATH, True), id="write"),
        param("toggle", (MUTE_PATH,), id="toggle"),
    ],
)
async def test_service_rejected(store, service, operation, args):
    store.reject = True
    with pytest.raises(
        mixer.Rejected, match=f"did not accept the change to {MUTE_PATH}"
    ):
        await _call(service, operation, *args)
    assert store.data[MUTE_PATH] == 0.0
