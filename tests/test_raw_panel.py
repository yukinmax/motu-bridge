"""RAW panel messages, incoming requests and the TCP connection."""

import asyncio
import json
import socket

import pytest
from pytest import param

import raw_panel

# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "normalized"),
    [
        param("Down", "Down", id="plain"),
        param("State:Down", "Down", id="prefixed"),
        param("Abs:600", "600", id="absolute"),
    ],
)
def test_normalize_hwc_value(value, normalized):
    assert raw_panel.normalize_hwc_value(value) == normalized


MODE_4 = {"HWCMode": {"State": 4}}
COLOR_8 = {"HWCColor": {"ColorIndex": {"Index": 8}}}


@pytest.mark.parametrize(
    ("messages", "batched"),
    [
        param([], None, id="none"),
        param([{"HWCIDs": [1], **MODE_4}], {"HWCIDs": [1], **MODE_4}, id="one"),
        param(
            [{"HWCIDs": [1], **MODE_4}, {"HWCIDs": [2], **MODE_4}],
            {"HWCIDs": [1, 2], **MODE_4},
            id="same-body",
        ),
        param(
            [
                {"HWCIDs": [1], **MODE_4},
                {"HWCIDs": [2], **COLOR_8},
                {"HWCIDs": [3], **MODE_4},
            ],
            [{"States": [{"HWCIDs": [1, 3], **MODE_4}, {"HWCIDs": [2], **COLOR_8}]}],
            id="different-bodies",
        ),
    ],
)
def test_batch_hwc(messages, batched):
    assert raw_panel.RawPanel.batch_hwc(messages) == batched


@pytest.mark.parametrize(
    ("builder", "args", "kwargs", "message"),
    [
        param("hwc_mode", (1,), {}, {"HWCIDs": [1], "HWCMode": {}}, id="mode-off"),
        param(
            "hwc_mode",
            (1,),
            {"state": 4, "blink_pattern": 2, "output": True},
            {"HWCIDs": [1], "HWCMode": {"State": 4, "BlinkPattern": 2, "Output": True}},
            id="mode",
        ),
        param(
            "hwc_fader",
            (13, 0),
            {},
            {"HWCIDs": [13], "HWCExtended": {"Interpretation": 5}},
            id="fader-bottom",
        ),
        param(
            "hwc_fader",
            (13, 645),
            {},
            {"HWCIDs": [13], "HWCExtended": {"Interpretation": 5, "Value": 645}},
            id="fader",
        ),
        param(
            "hwc_color",
            (1,),
            {"index": 8},
            {"HWCIDs": [1], **COLOR_8},
            id="color-index",
        ),
        param(
            "hwc_color",
            (1,),
            {"index": 8, "red": {"Value": 1}},
            {"HWCIDs": [1], "HWCColor": {"ColorRGB": {"Red": {"Value": 1}}}},
            id="color-rgb-wins",
        ),
        param(
            "hwc_text",
            (29,),
            {"title": "Mic", "text1": " -5.0", "solid_header": True, "formatting": 7},
            {
                "HWCIDs": [29],
                "HWCText": {
                    "Title": "Mic",
                    "SolidHeaderBar": True,
                    "TextLine1": " -5.0",
                    "Formatting": 7,
                },
            },
            id="text",
        ),
        param(
            "hwc_meter",
            (37,),
            {},
            {"HWCIDs": [37], "Processors": {"Audiometer": {}}},
            id="meter-empty",
        ),
        param(
            "hwc_meter",
            (37,),
            {"mono": True, "data1": 500, "peak1": 600},
            {
                "HWCIDs": [37],
                "Processors": {
                    "Audiometer": {
                        "Mono": True,
                        "Data1": 500,
                        "Peak1": 600,
                        "W": 176,
                        "H": 32,
                    }
                },
            },
            id="meter",
        ),
        param("cmd_wakeup", (), {}, [{"Command": {"WakeUp": True}}], id="wakeup"),
        param(
            "cmd_set_sleep_timeout",
            (0,),
            {},
            [{"Command": {"SetSleepTimeout": {}}}],
            id="sleep-timeout-default",
        ),
        param(
            "cmd_set_sleep_timeout",
            (600000,),
            {},
            [{"Command": {"SetSleepTimeout": {"Value": 600000}}}],
            id="sleep-timeout",
        ),
        param(
            "cmd_set_panel_brightness",
            (),
            {"leds": 4, "oleds": 0},
            [{"Command": {"PanelBrightness": {"LEDs": 4}}}],
            id="brightness",
        ),
    ],
)
def test_message_builders(panel, builder, args, kwargs, message):
    assert getattr(panel, builder)(*args, **kwargs) == message


# ---------------------------------------------------------------------------
# Requests from the panel
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("request_line", "attribute", "value"),
    [
        param("_model=SK_WAVEBOARD", "info", {"model": "SK_WAVEBOARD"}, id="info"),
        param(
            "_sleepTimer=600000", "info", {"panel_sleep_timeout": 600000}, id="sleep"
        ),
        param("map=13:1", "_panel_map", {"13": "1"}, id="map"),
        param(
            '_panelTopology_HWC={"HWc": []}',
            "_panel_topology",
            {"HWC": {"HWc": []}},
            id="topology",
        ),
    ],
)
async def test_handle_request(panel, request_line, attribute, value):
    await panel.handle_request(request_line)
    state = getattr(panel, attribute)
    assert {k: state[k] for k in value} == value


@pytest.mark.parametrize(
    "request_line",
    [
        param("", id="empty"),
        param("nack", id="nack"),
        param("garbage", id="invalid"),
        param("Unknown=1", id="unknown-command"),
    ],
)
async def test_handle_request_ignores(panel, request_line):
    await panel.handle_request(request_line)
    assert set(panel.info.values()) == {None}
    assert panel._hw_change_buffer == {}


async def test_handle_request_wake_inits_feedback(panel, recorder, monkeypatch):
    monkeypatch.setattr(raw_panel, "_WAKE_FEEDBACK_DELAY", 0)
    panel.set_feedback_controller(recorder)
    await panel.handle_request("_isSleeping=1")
    assert (panel.info["isSleeping"], recorder.inits) == (True, 0)
    await panel.handle_request("_isSleeping=0")
    assert (panel.info["isSleeping"], recorder.inits) == (False, 1)


async def test_process_buffers_sends_the_latest_change(panel, recorder):
    """Two moves of one control before the buffer runs reach the change
    controller as one change with the latest value, after a wakeup."""
    panel.set_change_controller(recorder)
    await panel.handle_request("HWC#13=Abs:500")
    await panel.handle_request("HWC#13=Abs:600")
    task = asyncio.create_task(panel.process_buffers())
    try:
        async with asyncio.timeout(1):
            await recorder.changed.wait()
    finally:
        panel.stop()
        await task
    assert recorder.changes == [({"hwcid": "13", "value": "600"}, "raw_panel", None)]
    assert panel.sent == [panel.cmd_wakeup()]


@pytest.mark.parametrize("panel", [{"delay": 0.2}], indirect=True, ids=["delay"])
async def test_process_buffers_waits_for_the_delay(panel, recorder):
    """A change stays in the buffer for `delay` seconds before it is sent."""
    panel.set_change_controller(recorder)
    await panel.handle_request("HWC#13=Abs:600")
    task = asyncio.create_task(panel.process_buffers())
    try:
        await asyncio.sleep(0.1)
        assert recorder.changes == []
        async with asyncio.timeout(1):
            await recorder.changed.wait()
    finally:
        panel.stop()
        await task


@pytest.mark.parametrize(
    "panel", [{"wakeup_min_interval": 60}], indirect=True, ids=["interval"]
)
async def test_wakeup_is_throttled(panel):
    await panel.wakeup(force=True)
    await panel.wakeup()
    await panel.wakeup(force=True)
    assert panel.sent == [panel.cmd_wakeup()] * 2


# ---------------------------------------------------------------------------
# Connection, over a local TCP server
# ---------------------------------------------------------------------------


async def test_connect_initializes_and_receives(panel_settings):
    """A local server stands in for the panel. It reads the 9 init commands a
    real RawPanel sends on connect, answers one line and hangs up, so receive()
    returns that line, then None once the connection is gone."""
    received: list[str] = []
    accepted = asyncio.Event()

    async def serve(reader, writer):
        for _ in range(9):
            received.append((await reader.readline()).decode().strip())
        writer.write(b"_model=SK_WAVEBOARD\r\n")
        await writer.drain()
        accepted.set()
        writer.close()

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    panel = raw_panel.RawPanel(**{**panel_settings, "host": "127.0.0.1", "port": port})
    try:
        await panel.connect(retries=0)
        assert panel.connected
        async with asyncio.timeout(1):
            await accepted.wait()
        assert await panel.receive() == "_model=SK_WAVEBOARD"
        assert await panel.receive() is None
        assert not panel.connected
    finally:
        panel.stop()
        await panel.disconnect()
        server.close()
        await server.wait_closed()
    assert received[0] == json.dumps(panel.cmd_get_panel_info(), separators=(",", ":"))
    assert len(received) == 9


async def test_connect_gives_up_after_retries(panel_settings):
    # A port that was free a moment ago, with nothing listening on it.
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    panel = raw_panel.RawPanel(**{**panel_settings, "host": "127.0.0.1", "port": port})
    await panel.connect(retries=0)
    assert not panel.connected
