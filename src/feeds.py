"""The change-handler contract shared by the MOTU endpoints, the panel and the
controllers that connect them.

The MOTU endpoints (motu._Endpoint) and the panel (raw_panel.RawPanel) are
each given a handler with set_change_controller() and call handler.on_change()
for each change, naming the feed it came from.
"""

from __future__ import annotations

from typing import Protocol

# Change feeds, passed as on_change(feed=...).
FEED_RAW_PANEL = "raw_panel"  # HWC event: data = {"hwcid": str, "value": str}
FEED_DATASTORE = "datastore"  # MOTU datastore diff: path -> value
FEED_METERS = "meters"  # MOTU meters diff: path -> samples


class ChangeHandler(Protocol):
    """Receives changes from one or more feeds."""

    async def on_change(
        self, data: dict, *, feed: str, origin: str | None = None
    ) -> None:
        """Handle data changed on feed.

        origin names what caused a datastore change, such as the hwcid of the
        panel control that wrote it, so feedback can skip that control.
        """
        ...
