"""Shared fakes for the MOTU, its HTTP API and the panel."""

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

import motu
import raw_panel

DATASTORE = json.loads((Path(__file__).parent / "fixtures/datastore.json").read_text())

ACCEPTED = motu._HttpResult(204, "No Content")


class FakeStore:
    """An in-memory mixer.Store that records writes.

    With reject set, writes fail like a MOTU that does not accept them.
    """

    def __init__(self, data: dict) -> None:
        self.data = data
        self.writes: list[tuple[str, Any]] = []
        self.reject = False

    def get(self, path: str) -> Any | None:
        return self.data.get(path)

    async def set(self, path: str, value: Any, *, origin: str | None = None) -> Any:
        self.writes.append((path, value))
        if self.reject:
            return None
        self.data[path] = value
        return value

    async def toggle(self, path: str) -> float | None:
        current = self.get(path)
        return None if current is None else await self.set(path, float(not current))


class FakeHTTP:
    """A motu.HTTPClient that records requests.

    Returns the queued responses in order, then accepts every request.
    """

    def __init__(self) -> None:
        self.responses: list[motu._HttpResult | None] = []
        self.requests: list[dict] = []

    async def request(self, url: str, **kwargs) -> motu._HttpResult | None:
        self.requests.append({"url": url, **kwargs})
        return self.responses.pop(0) if self.responses else ACCEPTED


class Recorder:
    """A change and feedback controller that records its calls."""

    def __init__(self) -> None:
        self.changes: list[tuple[dict, str, str | None]] = []
        self.changed = asyncio.Event()
        self.inits = 0

    async def on_change(self, data: dict, *, source: str, origin=None) -> None:
        self.changes.append((data, source, origin))
        self.changed.set()

    async def init_feedback(self) -> None:
        self.inits += 1


class RecordingPanel(raw_panel.RawPanel):
    """A RawPanel that records what it would send instead of connecting."""

    def __init__(self, **settings) -> None:
        super().__init__(**settings)
        self.sent: list = []

    async def send(self, message) -> None:
        self.sent.append(message)


@pytest.fixture
def store() -> FakeStore:
    """A store loaded with tests/fixtures/datastore.json."""
    return FakeStore(dict(DATASTORE))


@pytest.fixture
def http() -> FakeHTTP:
    return FakeHTTP()


@pytest.fixture
def recorder() -> Recorder:
    return Recorder()


@pytest.fixture
def panel_settings() -> dict:
    """RawPanel settings with no waits, for an unreachable host."""
    return {
        "host": "panel.invalid",
        "port": 9923,
        "delay": 0,
        "idle_sleep": 0.001,
        "sleep_timeout": 0,
        "sleep_mode": 0,
        "sleep_screen_saver": 0,
        "panel_brightness_leds": 4,
        "panel_brightness_oleds": 2,
        "panel_dimmed_gain": 20,
        "connect_retry_interval": 0,
        "connect_timeout": 1,
        "send_timeout": 1,
        "wakeup_min_interval": 0,
    }


@pytest.fixture
def panel(request, panel_settings) -> RecordingPanel:
    """A RecordingPanel; override settings with indirect parametrization."""
    return RecordingPanel(**{**panel_settings, **getattr(request, "param", {})})
