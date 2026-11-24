"""REST API routes through Quart's test client, over the fake store."""

import json
import math
from dataclasses import dataclass

import pytest
from pytest import param

import api

MIX = "/api/v1/motu/mix"
MUTE = f"{MIX}/chan/0/matrix/mute"
SEND = f"{MIX}/chan/0/matrix/aux/0/send"
EQ_GAIN = f"{MIX}/chan/0/eq/mid1/gain"
PEAK = f"{MIX}/chan/0/comp/peak"
FORMAT = f"{MIX}/chan/0/config/format"
THRESHOLD = f"{MIX}/chan/0/gate/threshold"

NOT_FOUND = "The requested URL was not found on the server."
NO_SUCH_PATH = "No such MOTU path: mix/"
NOT_AN_OBJECT = "Request body must be a JSON object (Content-Type: application/json)"
NOT_FINITE = "'value' must be a finite number"
NOT_A_BOOL = "'value' must be true or false"
NO_SCALE = "has no 'scale' option"
SCALES = "'scale' must be one of: linear, db, fader"
NOT_ON_OFF = "is not an on/off control"
NOT_LOADED = "MOTU data has not been loaded yet"


@dataclass(frozen=True)
class Raw:
    """A request body sent as is, for JSON that json= can't produce."""

    data: str
    content_type: str | None = "application/json"


@pytest.fixture
def store(store, monkeypatch):
    """The fake store behind the app's datastore, so no request reaches a MOTU."""
    monkeypatch.setattr(api.motu_ds, "data", store.data)
    monkeypatch.setattr(api.motu_ds, "set", store.set)
    return store


@pytest.fixture
def client(store):
    return api.app.test_client()


def _path(url: str) -> str:
    """The datastore path of a mixer URL."""
    return "mix/" + url.removeprefix(f"{MIX}/").split("?")[0]


def _reject_constant(constant: str):
    raise ValueError(f"{constant} is not JSON")


async def request(client, method: str, url: str, body=None) -> tuple[int, dict]:
    """Status and strictly parsed JSON body; errors must be {"error": message}."""
    if isinstance(body, Raw):
        headers = {"Content-Type": body.content_type} if body.content_type else {}
        response = await client.open(
            url, method=method, data=body.data, headers=headers
        )
    elif body is not None:
        response = await client.open(url, method=method, json=body)
    else:
        response = await client.open(url, method=method)
    assert response.mimetype == "application/json"
    data = json.loads(await response.get_data(), parse_constant=_reject_constant)
    if response.status_code >= 400:
        assert set(data) == {"error"}
    return response.status_code, data


# ---------------------------------------------------------------------------
# Success
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "response"),
    [
        param(MUTE, {"value": False}, id="bool"),
        param(PEAK, {"value": 0}, id="enum"),
        param(EQ_GAIN, {"value": -0.14, "unit": "dB"}, id="number"),
        param(
            f"{MIX}/monitor/0/override", {"value": 1024.0, "unit": None}, id="no-unit"
        ),
        param(FORMAT, {"value": "1:0"}, id="read-only"),
        param(f"{MIX}/ctrls/eq/mid1/gain", {"value": "real:0:-20:20:dB"}, id="spec"),
        param(SEND, {"value": 1.0, "scale": "linear"}, id="level"),
        param(f"{SEND}?scale=db", {"value": 0.0, "scale": "db"}, id="db"),
        # The panel fader puts 0 dB at 750.
        param(f"{SEND}?scale=fader", {"value": 750, "scale": "fader"}, id="fader"),
        param(
            f"{MIX}/chan/0/matrix/aux/1/send?scale=db",
            {"value": None, "scale": "db"},
            id="db-silence-null",
        ),
        param(
            f"{MIX}/talkback/dim?scale=db",
            {"value": 20 * math.log10(0.5), "scale": "db"},
            id="talkback-db",
        ),
        param(f"{SEND}?format=db", {"value": 1.0, "scale": "linear"}, id="other-query"),
    ],
)
async def test_get(client, url, response):
    assert await request(client, "GET", url) == (200, response)


@pytest.mark.parametrize(
    ("url", "body", "response", "stored"),
    [
        param(MUTE, {"value": True}, {"value": True}, 1.0, id="bool"),
        param(MUTE, {"value": True, "x": 1}, {"value": True}, 1.0, id="extra-key"),
        param(PEAK, {"value": 1}, {"value": 1}, 1.0, id="enum"),
        param(
            EQ_GAIN, {"value": -20}, {"value": -20.0, "unit": "dB"}, -20, id="number"
        ),
        param(
            SEND,
            {"value": 0.25},
            {"value": 0.25, "scale": "linear"},
            0.25,
            id="level",
        ),
        param(
            SEND,
            {"value": -20, "scale": "db"},
            {"value": -20.0, "scale": "db"},
            0.1,
            id="db",
        ),
        param(
            SEND,
            {"value": None, "scale": "db"},
            {"value": None, "scale": "db"},
            0.0,
            id="db-null-silence",
        ),
        param(
            f"{SEND}?scale=fader",
            {"value": 12, "scale": "db"},
            {"value": 1000, "scale": "fader"},
            round(10**0.6, 6),
            id="response-scale",
        ),
        param(
            f"{SEND}?scale=db",
            {"value": 1.0},
            {"value": 0.0, "scale": "db"},
            1.0,
            id="response-scale-only",
        ),
        param(
            SEND,
            {"value": 0.5, "format": "db"},
            {"value": 0.5, "scale": "linear"},
            0.5,
            id="other-key",
        ),
        param(
            THRESHOLD,
            {"value": 2},
            {"value": 1.0, "scale": "linear"},
            1.0,
            id="threshold-clamps",
        ),
    ],
)
async def test_put(client, store, url, body, response, stored):
    assert await request(client, "PUT", url, body) == (200, response)
    assert store.writes == [(_path(url), stored)]


@pytest.mark.parametrize(
    ("url", "body", "response", "stored"),
    [
        param(MUTE, None, {"value": True}, 1.0, id="on"),
        param(f"{MIX}/chan/1/matrix/mute", None, {"value": False}, 0.0, id="off"),
        param(f"{MUTE}?scale=db", None, {"value": True}, 1.0, id="query-ignored"),
        param(MUTE, Raw("{garbage"), {"value": True}, 1.0, id="body-ignored"),
    ],
)
async def test_post(client, store, url, body, response, stored):
    assert await request(client, "POST", url, body) == (200, response)
    assert store.writes == [(_path(url), stored)]


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "url", "body", "status", "message"),
    [
        # Unknown paths and routes
        param(
            "GET",
            f"{MIX}/chan/48/matrix/mute",
            None,
            404,
            NO_SUCH_PATH,
            id="get-unknown",
        ),
        param("GET", f"{MIX}/chan/0", None, 404, NO_SUCH_PATH, id="get-strip"),
        param("GET", f"{MUTE}/", None, 404, NO_SUCH_PATH, id="get-trailing-slash"),
        param(
            "GET",
            f"{MIX}/aux/0/comp/enable",
            None,
            404,
            NO_SUCH_PATH,
            id="get-missing-section",
        ),
        param("GET", MIX, None, 404, NOT_FOUND, id="get-mix-root"),
        param("GET", "/api/nope", None, 404, NOT_FOUND, id="get-api-unknown"),
        param(
            "PUT",
            f"{MIX}/chan/48/matrix/mute",
            {"value": True},
            404,
            NO_SUCH_PATH,
            id="put-unknown",
        ),
        param("POST", f"{MUTE}/toggle", None, 404, NO_SUCH_PATH, id="post-unknown"),
        # Scales
        param("GET", f"{SEND}?scale=", None, 400, SCALES, id="get-empty-scale"),
        param("GET", f"{MUTE}?scale=linear", None, 400, NO_SCALE, id="get-bool-scale"),
        param("GET", f"{EQ_GAIN}?scale=", None, 400, NO_SCALE, id="get-number-scale"),
        param("PUT", f"{SEND}?scale=", {"value": 1}, 400, SCALES, id="put-empty-scale"),
        param(
            "PUT",
            f"{SEND}?scale=volts",
            {"value": 1},
            400,
            SCALES,
            id="put-unknown-response-scale",
        ),
        param(
            "PUT",
            SEND,
            {"value": 1, "scale": "volts"},
            400,
            SCALES,
            id="put-unknown-scale",
        ),
        param(
            "PUT",
            SEND,
            {"value": 1, "scale": 5},
            400,
            SCALES,
            id="put-non-string-scale",
        ),
        param(
            "PUT",
            f"{EQ_GAIN}?scale=db",
            {"value": 1},
            400,
            NO_SCALE,
            id="put-number-scale",
        ),
        param(
            "PUT",
            EQ_GAIN,
            {"value": 1, "scale": "db"},
            400,
            NO_SCALE,
            id="put-number-body-scale",
        ),
        # Bodies
        param("PUT", MUTE, None, 400, NOT_AN_OBJECT, id="no-body"),
        param(
            "PUT", MUTE, Raw('{"value": true}', None), 400, NOT_AN_OBJECT, id="no-type"
        ),
        param("PUT", MUTE, Raw("{nope"), 400, NOT_AN_OBJECT, id="invalid-json"),
        param("PUT", MUTE, Raw("null"), 400, NOT_AN_OBJECT, id="null"),
        param("PUT", MUTE, [1], 400, NOT_AN_OBJECT, id="list"),
        param("PUT", MUTE, "x", 400, NOT_AN_OBJECT, id="string"),
        param("PUT", MUTE, {}, 400, "Missing field 'value'", id="no-value"),
        param("PUT", MUTE, {"muted": True}, 400, "Missing field 'value'", id="old-key"),
        # Values
        param("PUT", MUTE, {"value": 1}, 400, NOT_A_BOOL, id="bool-int"),
        param("PUT", MUTE, {"value": None}, 400, NOT_A_BOOL, id="bool-null"),
        param("PUT", SEND, {"value": None}, 400, NOT_FINITE, id="linear-null"),
        param(
            "PUT",
            SEND,
            {"value": None, "scale": "fader"},
            400,
            NOT_FINITE,
            id="fader-null",
        ),
        param(
            "PUT",
            SEND,
            Raw('{"value": NaN, "scale": "db"}'),
            400,
            NOT_FINITE,
            id="nan",
        ),
        param(
            "PUT",
            SEND,
            Raw('{"value": Infinity, "scale": "db"}'),
            400,
            NOT_FINITE,
            id="infinity",
        ),
        param(
            "PUT",
            SEND,
            Raw('{"value": -Infinity, "scale": "db"}'),
            400,
            NOT_FINITE,
            id="minus-infinity",
        ),
        param(
            "PUT",
            SEND,
            Raw('{"value": -1e999, "scale": "db"}'),
            400,
            NOT_FINITE,
            id="float-overflow",
        ),
        param("PUT", FORMAT, {"value": "2:0"}, 400, "is read-only", id="read-only"),
        param(
            "PUT",
            f"{MIX}/ctrls/eq/mid1/gain",
            {"value": "x"},
            400,
            "is read-only",
            id="spec",
        ),
        # Toggle
        param("POST", SEND, None, 400, NOT_ON_OFF, id="toggle-level"),
        param("POST", FORMAT, None, 400, "is read-only", id="toggle-read-only"),
    ],
)
async def test_errors(client, store, method, url, body, status, message):
    code, response = await request(client, method, url, body)
    assert code == status
    assert message in response["error"]
    assert not store.writes


@pytest.mark.parametrize(
    ("method", "body", "status", "message"),
    [
        param("GET", None, 503, NOT_LOADED, id="get"),
        param("PUT", {"value": True}, 503, NOT_LOADED, id="put"),
        param("POST", None, 503, NOT_LOADED, id="post"),
        param("PUT", {}, 400, "Missing field 'value'", id="bad-body-first"),
    ],
)
async def test_errors_before_load(client, store, method, body, status, message):
    store.data.clear()
    assert await request(client, method, MUTE, body) == (status, {"error": message})


@pytest.mark.parametrize(
    ("method", "url", "body"),
    [
        param("PUT", MUTE, {"value": True}, id="put"),
        param("POST", MUTE, None, id="post"),
        param("PUT", SEND, {"value": 0.5}, id="put-level"),
    ],
)
async def test_errors_rejected(client, store, method, url, body):
    store.reject = True
    before = dict(store.data)
    assert await request(client, method, url, body) == (
        502,
        {"error": f"MOTU did not accept the change to {_path(url)}"},
    )
    assert store.data == before


async def test_errors_unexpected(client, monkeypatch):
    async def fail(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(api.motu_ds, "set", fail)
    status, response = await request(client, "PUT", MUTE, {"value": True})
    assert status == 500
    assert "internal error" in response["error"]


@pytest.mark.parametrize("method", ["PATCH", "DELETE"])
@pytest.mark.parametrize(
    "url", [param(MUTE, id="known"), param(f"{MIX}/chan/99/matrix/mute", id="unknown")]
)
async def test_errors_method_not_allowed(client, method, url):
    response = await client.open(url, method=method, json={"value": True})
    assert (response.status_code, response.mimetype) == (405, "application/json")
    assert await response.get_json() == {
        "error": "The method is not allowed for the requested URL."
    }
    allow = {m.strip() for m in response.headers["Allow"].split(",")}
    assert {"GET", "PUT", "POST"} <= allow
    assert method not in allow


# ---------------------------------------------------------------------------
# Routes outside the API
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "url", "status", "mimetype"),
    [
        param("GET", "/", 200, "text/html", id="home"),
        param("GET", "/metrics", 200, "text/plain", id="metrics"),
        param("GET", "/nope", 404, "text/html", id="not-found"),
        param("POST", "/", 405, "text/html", id="method-not-allowed"),
    ],
)
async def test_non_api_routes(client, method, url, status, mimetype):
    response = await client.open(url, method=method)
    assert (response.status_code, response.mimetype) == (status, mimetype)
