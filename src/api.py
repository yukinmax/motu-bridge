import logging
import math

from aioprometheus import MetricsMiddleware
from aioprometheus.asgi.quart import metrics
from quart import Quart, request
from werkzeug.exceptions import BadRequest, HTTPException

import binding
import mappings
import mixer
import motu
import raw_panel
from settings import settings

MIX_ROUTE = "/api/v1/motu/mix/<path:path>"
# REST API status for each MixerError.
ERROR_STATUS: dict[type[mixer.MixerError], int] = {
    mixer.NotLoaded: 503,
    mixer.NotFound: 404,
    mixer.Rejected: 502,
    mixer.ReadOnly: 400,
    mixer.InvalidValue: 400,
    mixer.NotToggleable: 400,
}

logging.basicConfig(
    level=settings.log_level,
    format="%(levelname)s [%(name)s] %(message)s",
)

logger = logging.getLogger(__name__)
if settings.log_level != "INFO":
    logger.log(
        logging.getLevelNamesMapping()[settings.log_level],
        "Log level set to %s",
        settings.log_level,
    )

app = Quart("MOTU API")
app.config["BACKGROUND_TASK_SHUTDOWN_TIMEOUT"] = settings.shutdown_timeout
motu_http_client = motu.HTTPClient(
    retry_interval=settings.motu_retry_interval,
    connector_limit=settings.motu_connector_limit,
)
motu_ds = motu.DataStore(
    motu_http_client,
    host=settings.motu_host,
    port=settings.motu_port,
    refresh_timeout=settings.motu_refresh_timeout,
    refresh_retries=settings.motu_refresh_retries,
    mutate_timeout=settings.motu_mutate_timeout,
    mutate_retries=settings.motu_mutate_retries,
)
motu_ms = motu.Meters(
    motu_http_client,
    host=settings.motu_host,
    port=settings.motu_port,
    refresh_timeout=settings.motu_refresh_timeout,
    refresh_retries=settings.motu_refresh_retries,
)
skaarhoj_panel = raw_panel.RawPanel(
    host=settings.panel_host,
    port=settings.panel_port,
    delay=settings.panel_delay,
    idle_sleep=settings.panel_idle_sleep,
    sleep_timeout=settings.panel_sleep_timeout,
    sleep_mode=settings.panel_sleep_mode,
    sleep_screen_saver=settings.panel_sleep_screen_saver,
    panel_brightness_leds=settings.panel_brightness_leds,
    panel_brightness_oleds=settings.panel_brightness_oleds,
    panel_dimmed_gain=settings.panel_dimmed_gain,
    connect_retry_interval=settings.panel_connect_retry_interval,
    connect_timeout=settings.panel_connect_timeout,
    send_timeout=settings.panel_send_timeout,
    wakeup_min_interval=settings.panel_wakeup_min_interval,
)

# Shared by the REST API and the panel.
mixer_service = mixer.MixerService(motu_ds, scales=binding.SCALES)

control_map = binding.ControlMap.from_raw(mappings.control_map)
feedback_map = binding.FeedbackMap.from_raw(mappings.feedback_map)

motu_controller = binding.MotuController(control_map, mixer_service)
panel_controller = binding.PanelController(
    feedback_map,
    panel=skaarhoj_panel,
    datastore=motu_ds,
    meters=motu_ms,
    fader_taper=binding.FADER_TAPER,
    meter_taper=binding.METER_TAPER,
)

skaarhoj_panel.set_change_handler(motu_controller)
skaarhoj_panel.set_feedback_handler(panel_controller)
motu_ds.set_change_handler(panel_controller)
motu_ms.set_change_handler(panel_controller)

app.asgi_app = MetricsMiddleware(app.asgi_app)
app.add_url_rule("/metrics", "metrics", metrics, methods=["GET"])


@app.before_serving
async def startup() -> None:
    await motu_http_client.start()
    await motu_ds.refresh(handle_changes=False)
    await motu_ms.refresh(handle_changes=False)
    logger.info("Initial data refresh has completed")
    await skaarhoj_panel.connect(retries=settings.panel_connect_retries)
    app.add_background_task(skaarhoj_panel.handle_requests)
    app.add_background_task(skaarhoj_panel.process_buffers)
    app.add_background_task(motu_ds.poll, diff_check=False)
    app.add_background_task(motu_ms.poll, idle_interval=settings.meters_idle_interval)


@app.after_serving
async def shutdown() -> None:
    logger.info("Shutting down...")
    skaarhoj_panel.stop()
    motu_ds.stop()
    motu_ms.stop()
    await skaarhoj_panel.disconnect()
    await motu_http_client.close()
    logger.info("Shutdown complete")


@app.route("/", methods=["GET"])
async def home() -> str:
    return "<h1>MOTU API</h1>"


@app.errorhandler(HTTPException)
async def handle_http_exception(
    error: HTTPException,
) -> HTTPException | tuple[dict, int, dict]:
    """Return Quart's own errors (404, 405, 500...) as JSON under /api/."""
    if not request.path.startswith("/api/"):
        return error
    headers = {
        name: value
        for name, value in error.get_headers()
        if name.lower() not in ("content-type", "content-length")
    }
    return {"error": error.description}, error.code or 500, headers


# ---------------------------------------------------------------------------
# MOTU mixer
# ---------------------------------------------------------------------------


@app.errorhandler(mixer.MixerError)
async def handle_mixer_error(error: mixer.MixerError) -> tuple[dict, int]:
    return {"error": str(error)}, ERROR_STATUS.get(type(error), 500)


def _from_json(value: object) -> object:
    """JSON has no infinity: null is silence (-inf dB).

    Infinity and NaN, which the JSON parser also accepts, are not valid JSON.
    """
    if value is None:
        return -math.inf
    if isinstance(value, float) and not math.isfinite(value):
        raise BadRequest("'value' must be a finite number")
    return value


def _to_json(value: object) -> object:
    """JSON has no infinity: silence (-inf dB) is null."""
    return None if isinstance(value, float) and not math.isfinite(value) else value


def _response(reading: mixer.Reading) -> dict:
    return {"value": _to_json(reading.value), **reading.meta}


async def _body() -> dict:
    body = await request.get_json(silent=True)
    if not isinstance(body, dict):
        raise BadRequest(
            "Request body must be a JSON object (Content-Type: application/json)"
        )
    if "value" not in body:
        raise BadRequest("Missing field 'value'")
    return body


@app.route(MIX_ROUTE, methods=["GET"])
async def mix_get(path: str) -> dict:
    return _response(mixer_service.read(f"mix/{path}", request.args.get("scale")))


@app.route(MIX_ROUTE, methods=["PUT"])
async def mix_put(path: str) -> dict:
    body = await _body()
    return _response(
        await mixer_service.write(
            f"mix/{path}",
            _from_json(body["value"]),
            body.get("scale"),
            request.args.get("scale"),
        )
    )


@app.route(MIX_ROUTE, methods=["POST"])
async def mix_toggle(path: str) -> dict:
    return _response(await mixer_service.toggle(f"mix/{path}"))


if __name__ == "__main__":
    app.run(host=settings.api_host, port=settings.api_port)
