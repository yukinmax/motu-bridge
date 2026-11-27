"""Panel tapers, binding maps and the panel ↔ MOTU controllers."""

import math

import pytest
from pytest import param

import binding
import mixer
import motu
from feeds import FEED_METERS

SEND = "mix/chan/0/matrix/aux/0/send"
MUTE = "mix/chan/0/matrix/mute"
SOLO = "mix/chan/0/matrix/solo"
OVERRIDE = "mix/monitor/0/override"

# Panel hwcids. Buttons get their feedback on the hwcid that controls them.
FADER = 13
DISPLAY = 29
METER = 37
DOWN = "63.4"
LOUD_DOWN = "63.5"
MUTE_BUTTON = 1
SOLO_BUTTON = 2
OVERRIDE_BUTTON = 80
OTHER_OVERRIDE_BUTTON = 81

# Fader positions and gains worked out from the tapers' breakpoints:
# FADER_TAPER is linear in dB from 125 (-30 dB) to 1000 (+12 dB),
# METER_TAPER from 1 (-60 dB) to 1000 (+12 dB).
FADER_UNITY = 750  # 125 + 875 * 30 / 42
FADER_MINUS_5_DB = 645  # 125 + 875 * 25 / 42 = 645.8
FADER_TOP = 1000
METER_UNITY = 833  # 1 + 999 * 60 / 72 = 833.5
METER_MINUS_6_DB = 749  # gain 0.5: 1 + 999 * 53.98 / 72 = 749.96
MINUS_5_DB = 0.562341  # 10 ** (-5 / 20), as the MOTU stores it
PLUS_12_DB = 3.981072  # 10 ** (12 / 20)

# ---------------------------------------------------------------------------
# Tapers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("taper", "position", "db"),
    [
        param(binding.FADER_TAPER, 0, -math.inf, id="fader-silence"),
        param(binding.FADER_TAPER, 1, -120, id="fader-1"),
        param(binding.FADER_TAPER, 5, -60, id="fader-5"),
        param(binding.FADER_TAPER, 125, -30, id="fader-125"),
        param(binding.FADER_TAPER, 1000, 12, id="fader-top"),
        param(binding.METER_TAPER, 0, -math.inf, id="meter-silence"),
        param(binding.METER_TAPER, 1, -60, id="meter-1"),
        param(binding.METER_TAPER, 1000, 12, id="meter-top"),
    ],
)
def test_taper_points(taper, position, db):
    assert taper.to_db(position) == db
    assert taper.from_db(db) == position


@pytest.mark.parametrize(
    ("method", "value", "result"),
    [
        param("to_db", -50, -math.inf, id="to-db-below"),
        param("to_db", 5000, 12, id="to-db-above"),
        param("from_db", -200, 0, id="from-db-below"),
        param("from_db", 40, 1000, id="from-db-above"),
    ],
)
def test_taper_clamps(method, value, result):
    assert getattr(binding.FADER_TAPER, method)(value) == result


def test_taper_round_trip():
    # from_db truncates, so a round trip lands on the position or one below.
    for position in range(1001):
        db = binding.FADER_TAPER.to_db(position)
        assert position - 1 <= binding.FADER_TAPER.from_db(db) <= position


# ---------------------------------------------------------------------------
# Maps
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "role"),
    [
        param(SEND, "send", id="send"),
        param("mix/main/0/matrix/fader", "send", id="fader"),
        param("mix/chan/0/matrix/mute", "mute", id="mute"),
        param("mix/monitor/0/override", "override", id="override"),
    ],
)
def test_role_from_path(path, role):
    assert binding.role_from_path(path) == role


@pytest.mark.parametrize(
    ("name", "kind"),
    [
        param("button", "button", id="button"),
        param("button2", "button", id="numbered-button"),
        param("fader", "fader", id="fader"),
        param("display", "display", id="display"),
        param("display3", "meter", id="meter"),
        param("knob", "unknown", id="unknown"),
    ],
)
def test_kind_from_name(name, kind):
    assert binding.kind_from_name(name) == kind


def test_control_map_from_raw():
    controls = binding.ControlMap.from_raw(
        {
            13: {"path": SEND},
            DOWN: {"path": SEND, "default_level": -5},
            "80.1": {"path": "mix/monitor/0/override", "override_index": 2},
        }
    )
    assert dict(controls) == {
        "13": binding.BindingConfig(SEND, "send"),
        DOWN: binding.BindingConfig(SEND, "send", default_level_db=-5.0),
        "80.1": binding.BindingConfig(
            "mix/monitor/0/override", "override", override_index=2
        ),
    }
    assert controls["80.1"].override_value == binding.OVERRIDE_BASE + 2


# ---------------------------------------------------------------------------
# Panel ↔ MOTU
# ---------------------------------------------------------------------------


FEEDBACK = {
    SEND: {
        "fader": {"hwcid": FADER, "mode": {"state": 4}},
        "display": {"hwcid": DISPLAY, "text": {"title": "Mic"}},
    },
    MUTE: {"button": {"hwcid": MUTE_BUTTON, "mode": {"state": (4, 0)}}},
    OVERRIDE: {
        "button": {
            "hwcid": OVERRIDE_BUTTON,
            "value": binding.OVERRIDE_BASE,
            "mode": {"state": (4, 0)},
        }
    },
}
CONTROLS = {
    str(FADER): {"path": SEND},
    DOWN: {"path": SEND, "default_level": -5},
    LOUD_DOWN: {"path": SEND, "default_level": 20},
    str(MUTE_BUTTON): {"path": MUTE},
    str(SOLO_BUTTON): {"path": SOLO},
    str(OVERRIDE_BUTTON): {"path": OVERRIDE, "override_index": 0},
    str(OTHER_OVERRIDE_BUTTON): {"path": OVERRIDE, "override_index": 1},
}


def _panel_controller(
    feedback: dict, *, panel, datastore, http, meters_data: dict | None = None
) -> binding.PanelController:
    """A PanelController with the default tapers and meters over http, loaded
    with meters_data."""
    meters = motu.Meters(
        http, host="motu", port=80, refresh_timeout=None, refresh_retries=None
    )
    meters.data = dict(meters_data or {})
    return binding.PanelController(
        binding.FeedbackMap.from_raw(feedback),
        panel=panel,
        datastore=datastore,
        meters=meters,
        fader_taper=binding.FADER_TAPER,
        meter_taper=binding.METER_TAPER,
    )


@pytest.fixture
def controller(store, http, panel) -> binding.MotuController:
    """Both controllers around a DataStore over the fake HTTP client, as api.py
    wires them. The DataStore shares the fake store's data."""
    ds = motu.DataStore(
        http,
        host="motu",
        port=80,
        refresh_timeout=None,
        refresh_retries=None,
        mutate_timeout=None,
        mutate_retries=None,
    )
    ds.data = store.data
    feedback = _panel_controller(FEEDBACK, panel=panel, datastore=ds, http=http)
    ds.set_change_handler(feedback)
    service = mixer.MixerService(ds, scales=binding.SCALES)
    return binding.MotuController(binding.ControlMap.from_raw(CONTROLS), service)


def _feedback(sent: list) -> list[tuple[int, str, object]]:
    """(hwcid, field, value) for each fader position, text line and button
    state sent to the panel, in order.

    A message is one state, or a batch [{"States": [...]}] from batch_hwc whose
    states may each list several hwcids. Commands such as wakeup are left out.
    """
    states = []
    for message in sent:
        if isinstance(message, list):
            states.extend(state for item in message for state in item.get("States", []))
        else:
            states.append(message)
    out = []
    for state in states:
        for hwcid in state["HWCIDs"]:
            if "HWCExtended" in state:
                out.append((hwcid, "fader", state["HWCExtended"].get("Value", 0)))
            if "HWCText" in state:
                out.append((hwcid, "text", state["HWCText"]["TextLine1"]))
            if "HWCMode" in state:
                out.append((hwcid, "mode", state["HWCMode"].get("State", 0)))
    return out


@pytest.mark.parametrize(
    ("initial", "events", "stored", "feedback"),
    [
        param(
            {},
            [(FADER, "600")],
            # 600 is -30 + 42 * 475 / 875 = -7.2 dB.
            {SEND: round(10 ** (-7.2 / 20), 6)},
            [(DISPLAY, "text", " -7.2")],
            id="fader-move-suppresses-its-echo",
        ),
        param(
            {},
            [(DOWN, "Down")],
            {SEND: MINUS_5_DB},
            [(FADER, "fader", FADER_MINUS_5_DB), (DISPLAY, "text", " -5.0")],
            id="down-sets-default",
        ),
        param(
            {},
            [(DOWN, "Down"), (DOWN, "Down")],
            {SEND: 1.0},
            [
                (FADER, "fader", FADER_MINUS_5_DB),
                (DISPLAY, "text", " -5.0"),
                (FADER, "fader", FADER_UNITY),
                (DISPLAY, "text", "  0.0"),
            ],
            id="down-again-sets-unity",
        ),
        param(
            {},
            [(FADER, "600"), (DOWN, "Down")],
            {SEND: MINUS_5_DB},
            [
                (DISPLAY, "text", " -7.2"),
                (FADER, "fader", FADER_MINUS_5_DB),
                (DISPLAY, "text", " -5.0"),
            ],
            id="down-after-fader-move-sends-fader-position",
        ),
        param(
            {},
            [(LOUD_DOWN, "Down")],
            {SEND: PLUS_12_DB},
            [(FADER, "fader", FADER_TOP), (DISPLAY, "text", " 12.0")],
            id="down-default-capped-at-plus-12",
        ),
        param({}, [("99", "Down")], {SEND: 1.0}, [], id="unmapped"),
        param({}, [(DOWN, "Up")], {SEND: 1.0}, [], id="up"),
        param(
            {},
            [(MUTE_BUTTON, "Down")],
            {MUTE: 1.0},
            [(MUTE_BUTTON, "mode", 4)],
            id="mute",
        ),
        param(
            {},
            [(MUTE_BUTTON, "Down"), (MUTE_BUTTON, "Down")],
            {MUTE: 0.0},
            [(MUTE_BUTTON, "mode", 4), (MUTE_BUTTON, "mode", 0)],
            id="mute-again-unmutes",
        ),
        param({}, [(MUTE_BUTTON, "Up")], {MUTE: 0.0}, [], id="mute-up"),
        param(
            {},
            [(SOLO_BUTTON, "Down")],
            {SOLO: 1.0, OVERRIDE: -1.0},
            [(OVERRIDE_BUTTON, "mode", 0)],
            id="solo-ends-override",
        ),
        param(
            {SOLO: 1.0},
            [(SOLO_BUTTON, "Down")],
            {SOLO: 1.0, OVERRIDE: -1.0},
            [(OVERRIDE_BUTTON, "mode", 0)],
            id="solo-during-override-stays-on",
        ),
        param(
            {SOLO: 1.0, OVERRIDE: -1.0},
            [(SOLO_BUTTON, "Down")],
            {SOLO: 0.0, OVERRIDE: -1.0},
            [(OVERRIDE_BUTTON, "mode", 0)],
            id="solo-toggles-without-override",
        ),
        param(
            {},
            [(OVERRIDE_BUTTON, "Down")],
            {OVERRIDE: -1.0},
            [(OVERRIDE_BUTTON, "mode", 0)],
            id="override-off",
        ),
        param(
            {OVERRIDE: -1.0},
            [(OVERRIDE_BUTTON, "Down")],
            {OVERRIDE: 1024.0},
            [(OVERRIDE_BUTTON, "mode", 4)],
            id="override-on",
        ),
        param(
            {},
            [(OTHER_OVERRIDE_BUTTON, "Down")],
            {OVERRIDE: 1025.0},
            [(OVERRIDE_BUTTON, "mode", 0)],
            id="override-other-input",
        ),
    ],
)
async def test_motu_controller_feedback(
    controller, panel, store, initial, events, stored, feedback
):
    """Starting from the fixture data updated with `initial`, send the panel
    events (hwcid, value) in order, then check the stored values and what the
    panel got back."""
    store.data.update(initial)
    for hwcid, value in events:
        await controller.process_hwc(str(hwcid), value)
    assert {path: store.data[path] for path in stored} == stored
    assert _feedback(panel.sent) == feedback


@pytest.mark.parametrize(
    ("hwcid", "value", "warning"),
    [
        param(FADER, "600", "Unable to set path", id="fader"),
        param(MUTE_BUTTON, "Down", "Unable to toggle path", id="toggle"),
        param(OVERRIDE_BUTTON, "Down", "Unable to set path", id="override"),
    ],
)
async def test_motu_controller_logs_rejected_writes(
    controller, http, panel, store, caplog, hwcid, value, warning
):
    """A write the MOTU rejects is logged; the store and panel are unchanged."""
    before = dict(store.data)
    http.responses = [None]
    await controller.process_hwc(str(hwcid), value)
    assert warning in caplog.text
    assert store.data == before
    assert panel.sent == []


@pytest.mark.parametrize(
    ("hwcid", "value", "warning"),
    [
        param(DOWN, "Down", "Unable to read path", id="down-read"),
        param(FADER, "600", "Unable to set path", id="fader-write"),
    ],
)
async def test_motu_controller_logs_unknown_paths(
    controller, panel, store, caplog, hwcid, value, warning
):
    """A mapped path the MOTU doesn't have is logged and nothing is written."""
    del store.data[SEND]
    await controller.process_hwc(str(hwcid), value)
    assert warning in caplog.text
    assert SEND not in store.data
    assert panel.sent == []


@pytest.mark.parametrize(
    ("pre_fader", "initial", "meter"),
    [
        param(
            True,
            {MUTE: 1.0},
            {"data1": METER_UNITY, "peak1": METER_UNITY},
            id="pre-fader-ignores-mute",
        ),
        param(
            False,
            {},
            {"data1": METER_UNITY, "peak1": METER_UNITY},
            id="post-fader-unity",
        ),
        param(
            False,
            {SEND: 0.5},
            {"data1": METER_MINUS_6_DB, "peak1": METER_MINUS_6_DB},
            id="post-fader-scaled",
        ),
        param(False, {MUTE: 1.0}, {}, id="post-fader-muted"),
    ],
)
async def test_panel_controller_meters(store, panel, http, pre_fader, initial, meter):
    """One meters update at 0 dB on channel 0. A post-fader meter is scaled by
    the fader gain and drops to silence when muted."""
    store.data.update(initial)
    feedback = _panel_controller(
        {
            "mix/level": {
                # A "display<n>" target is a meter.
                "display1": {
                    "hwcid": METER,
                    "channels": [0],
                    "pre_fader": pre_fader,
                    "fader_path": SEND,
                    "mute_path": MUTE,
                    "audio_meter": {"mono": True},
                }
            }
        },
        panel=panel,
        datastore=store,
        http=http,
    )
    # A full-scale sample on channel 0 is 0 dB.
    await feedback.on_change(
        {"mix/level/0": [motu.METER_SCALE], "mix/level/peaks": [motu.METER_SCALE]},
        feed=FEED_METERS,
    )
    assert panel.sent == [
        panel.cmd_wakeup(),
        panel.hwc_meter(METER, mono=True, **meter),
    ]


async def test_panel_controller_init_feedback(store, panel, http, caplog):
    """init_feedback sends every mapped datastore value with its full style,
    then a meter frame even though nothing changed. A path the datastore lacks
    is logged once."""
    missing = "mix/chan/9/matrix/mute"
    feedback = _panel_controller(
        {
            **FEEDBACK,
            missing: {"button": {"hwcid": 99, "mode": {"state": (4, 0)}}},
            "mix/level": {
                "display1": {
                    "hwcid": METER,
                    "channels": [0],
                    "audio_meter": {"mono": True},
                }
            },
        },
        panel=panel,
        datastore=store,
        http=http,
        # Every slot and the peaks hold a full-scale sample on channel 0.
        meters_data={
            f"mix/level/{slot}": [motu.METER_SCALE]
            for slot in [*map(str, range(motu.METER_SLOTS)), "peaks"]
        },
    )
    await feedback.init_feedback()
    assert _feedback(panel.sent) == [
        (FADER, "fader", FADER_UNITY),
        (FADER, "mode", 4),
        (DISPLAY, "text", "  0.0"),
        (MUTE_BUTTON, "mode", 0),
        (OVERRIDE_BUTTON, "mode", 4),
    ]
    assert panel.sent[-2:] == [
        panel.cmd_wakeup(),
        panel.hwc_meter(METER, mono=True, data1=METER_UNITY, peak1=METER_UNITY),
    ]
    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert warnings == [f"Path {missing} is not available"]
