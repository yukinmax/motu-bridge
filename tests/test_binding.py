"""Panel tapers, binding maps and the panel ↔ MOTU controllers."""

import math

import pytest
from pytest import param

import binding
import mixer
import motu

SEND = "mix/chan/0/matrix/aux/0/send"
FADER = 13
DISPLAY = 29
DOWN = "63.4"
DOWN_DB = -5.0
DOWN_GAIN = round(mixer.db_to_gain(DOWN_DB), mixer.LEVEL_DECIMALS)

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


@pytest.fixture
def datastore(store, http, panel):
    """A DataStore over the fake HTTP client, wired to the panel like api.py."""
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
    feedback = binding.PanelController(
        binding.FeedbackMap.from_raw(
            {
                SEND: {
                    "fader": {"hwcid": FADER, "mode": {"state": 4}},
                    "display": {"hwcid": DISPLAY, "text": {"title": "Mic"}},
                }
            }
        ),
        fader_taper=binding.FADER_TAPER,
        meter_taper=binding.METER_TAPER,
    )
    feedback.set_panel(panel)
    feedback.set_datastore(ds)
    ds.set_change_controller(feedback)
    return ds


@pytest.fixture
def controller(datastore):
    controller = binding.MotuController(
        binding.ControlMap.from_raw(
            {
                str(FADER): {"path": SEND},
                DOWN: {"path": SEND, "default_level": DOWN_DB},
            }
        ),
        fader_taper=binding.FADER_TAPER,
    )
    controller.set_datastore(datastore)
    return controller


def _feedback(sent: list) -> dict[int, list]:
    """Fader positions and display lines sent to the panel, by hwcid."""
    out: dict[int, list] = {FADER: [], DISPLAY: []}
    for message in sent:
        for state in message[0]["States"] if isinstance(message, list) else [message]:
            if "HWCExtended" in state:
                out[FADER].append(state["HWCExtended"].get("Value", 0))
            if "HWCText" in state:
                out[DISPLAY].append(state["HWCText"]["TextLine1"])
    return out


@pytest.mark.parametrize(
    ("events", "stored", "feedback"),
    [
        param(
            [(str(FADER), "600")],
            mixer.db_to_gain(binding.FADER_TAPER.to_db(600)),
            {FADER: [], DISPLAY: [" -7.2"]},
            id="fader-move-suppresses-its-echo",
        ),
        param(
            [(DOWN, "Down")],
            DOWN_GAIN,
            {FADER: [645], DISPLAY: [" -5.0"]},
            id="down-sets-default",
        ),
        param(
            [(DOWN, "Down"), (DOWN, "Down")],
            1.0,
            {FADER: [645, 750], DISPLAY: [" -5.0", "  0.0"]},
            id="down-again-sets-unity",
        ),
        param(
            [(str(FADER), "600"), (DOWN, "Down")],
            DOWN_GAIN,
            {FADER: [645], DISPLAY: [" -7.2", " -5.0"]},
            id="down-after-fader-move-sends-fader-position",
        ),
        param([("99", "Down")], 1.0, {FADER: [], DISPLAY: []}, id="unmapped"),
        param([(DOWN, "Up")], 1.0, {FADER: [], DISPLAY: []}, id="up"),
    ],
)
async def test_motu_controller_feedback(
    controller, panel, store, events, stored, feedback
):
    for hwcid, value in events:
        await controller.process_hwc(hwcid, value)
    assert store.data[SEND] == stored
    assert _feedback(panel.sent) == feedback
