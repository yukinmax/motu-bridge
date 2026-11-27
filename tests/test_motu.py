"""MOTU endpoints over the fake HTTP client."""

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from pytest import param

import motu

MUTE = "mix/chan/0/matrix/mute"
NOT_MODIFIED = motu._HttpResult(304, "Not Modified")


def _ok(body: dict, etag: str = "1") -> motu._HttpResult:
    return motu._HttpResult(200, "OK", etag, body)


@pytest.fixture
def datastore(http, recorder) -> motu.DataStore:
    ds = motu.DataStore(
        http,
        host="motu",
        port=80,
        refresh_timeout=None,
        refresh_retries=None,
        mutate_timeout=2,
        mutate_retries=0,
    )
    ds.set_change_handler(recorder)
    return ds


@pytest.fixture
def meters(http, recorder) -> motu.Meters:
    ms = motu.Meters(
        http, host="motu", port=80, refresh_timeout=None, refresh_retries=None
    )
    ms.set_change_handler(recorder)
    return ms


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("old", "new", "diff"),
    [
        param(
            {"a": 1, "b": 2}, {"a": 1, "b": 3, "c": 4}, {"b": 3, "c": 4}, id="changed"
        ),
        param({"a": 1, "b": 2}, {"a": 1}, {}, id="unchanged"),
        param({}, {"a": 1}, {"a": 1}, id="new"),
    ],
)
def test_dict_diff(old, new, diff):
    assert motu.dict_diff(old, new) == diff


# ---------------------------------------------------------------------------
# HTTPClient, over a local aiohttp server
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("statuses", "result", "etags_sent"),
    [
        param([200], _ok({"a": 1}, "7"), ["5"], id="ok"),
        param([304], motu._HttpResult(304, "Not Modified", "7"), ["5"], id="304"),
        param([500, 200], _ok({"a": 1}, "7"), ["5", None], id="retry-drops-etag"),
        param([500, 500], None, ["5", None], id="gives-up"),
    ],
)
async def test_http_client_request(statuses, result, etags_sent):
    """The server answers with `statuses` in order and records the If-None-Match
    header of each attempt. The client sends ETag 5 and may retry once."""
    seen = []

    async def handler(request):
        seen.append(request.headers.get("If-None-Match"))
        status = statuses[len(seen) - 1]
        if status == 200:
            return web.json_response({"a": 1}, headers={"ETag": "7"})
        return web.Response(status=status, headers={"ETag": "7"})

    app = web.Application()
    app.router.add_get("/datastore", handler)
    http = motu.HTTPClient(retry_interval=0, connector_limit=1)
    async with TestServer(app) as server:
        await http.start()
        try:
            url = str(server.make_url("/datastore"))
            assert await http.request(url, etag="5", retries=1) == result
        finally:
            await http.close()
    assert seen == etags_sent


# ---------------------------------------------------------------------------
# DataStore
# ---------------------------------------------------------------------------


async def test_datastore_set_patches_and_notifies(datastore, http, recorder):
    assert await datastore.set(MUTE, 1.0, origin="7") == 1.0
    assert http.requests == [
        {
            "url": "http://motu:80/datastore",
            "params": {"client": datastore.client_id},
            "method": "PATCH",
            "data": b'json={"mix/chan/0/matrix/mute": 1.0}',
            "retries": 0,
            "attempt_timeout": 2,
        }
    ]
    assert datastore.data == {MUTE: 1.0}
    assert recorder.changes == [({MUTE: 1.0}, "datastore", "7")]


async def test_datastore_set_rejected(datastore, http, recorder):
    http.responses = [None]
    assert await datastore.set(MUTE, 1.0) is None
    assert datastore.data == {}
    assert recorder.changes == []


async def test_datastore_refresh_diffs_and_sends_etags(datastore, http, recorder):
    http.responses = [
        _ok({"a": 1, "b": 2}, "1"),
        _ok({"a": 1, "b": 3}, "2"),
        NOT_MODIFIED,
    ]
    assert await datastore.refresh(handle_changes=False) == {"a": 1, "b": 2}
    assert await datastore.refresh(diff_check=True) == {"b": 3}
    assert await datastore.refresh() is None
    assert [r["etag"] for r in http.requests] == [None, "1", "2"]
    assert datastore.data == {"a": 1, "b": 3}
    assert recorder.changes == [({"b": 3}, "datastore", None)]


async def test_datastore_refresh_failure_drops_the_etag(datastore, http):
    http.responses = [_ok({"a": 1}, "1"), None]
    await datastore.refresh()
    assert await datastore.refresh() is None
    assert datastore.etag is None


# ---------------------------------------------------------------------------
# Meters
# ---------------------------------------------------------------------------


def test_meters_peaks_and_previous_levels(meters):
    """Peaks are each channel's maximum over the level slots. current_index
    follows the slot in the latest diff; previous_levels is the slot before it."""
    meters.data = {"mix/level/0": [1, 9, 0], "mix/level/1": [5, 2, 0]}
    assert meters._update_peaks() == {"mix/level/peaks": [5, 9, 0]}
    assert meters.previous_levels is None
    meters._set_current_index({"mix/level/peaks": [], "mix/level/1": [5, 2, 0]})
    assert meters.current_index == 1
    assert meters.previous_levels == [1, 9, 0]


def test_meters_previous_levels_wrap(meters):
    meters.data = {f"mix/level/{motu.METER_SLOTS - 1}": [7]}
    meters.current_index = 0
    assert meters.previous_levels == [7]


@pytest.mark.parametrize(
    ("data", "idle"),
    [
        param({}, False, id="no-peaks"),
        param({"mix/level/peaks": []}, False, id="empty"),
        param({"mix/level/peaks": [0, 0]}, True, id="silent"),
        param({"mix/level/peaks": [0, 3]}, False, id="signal"),
    ],
)
def test_meters_idle(meters, data, idle):
    meters.data = data
    assert meters.idle is idle


async def test_meters_refresh_adds_peaks_and_notifies(meters, http, recorder):
    http.responses = [_ok({"mix/level/0": [1, 2], "mix/level/1": [3, 0]})]
    diff = await meters.refresh()
    assert diff == {
        "mix/level/0": [1, 2],
        "mix/level/1": [3, 0],
        "mix/level/peaks": [3, 2],
    }
    assert meters.current_index == 0
    assert http.requests[0]["params"] == {
        "meters": "mix/level",
        "client": meters.client_id,
    }
    assert recorder.changes == [(diff, "meters", None)]
