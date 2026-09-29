"""In-memory demo state: the tool-call event log, arrival alerts, and which
call is currently "active".

Per-call scoping, and its limit: AssemblyAI's HTTP tools call this server
directly from AssemblyAI's own infrastructure — nothing in agent.json's tool
definitions lets a call carry a session id through to /tools/*, so a tool
call arriving here has no idea which browser tab started it. The browser
DOES control its own requests (/api/call/start, /api/location, /api/events,
/api/map-state), so it mints a fresh call_id every time "Start call" is
pressed (start_call, below) and sends that id on everything it asks for.
Every tool-call event gets stamped with whichever call_id is "active" right
now (log_event/log_alert_event) — the simplest correlation available without
AssemblyAI's side cooperating — and a browser only ever gets back data
stamped with the call_id it asks for (events_since), so a fresh page load or
a stale tab can never see another call's trip, events or alerts.

The real limit this doesn't cover: there's only ONE "active" call_id at a
time. Two genuinely simultaneous callers would each have their tool-call
events attributed to whichever of them is currently "active", so one
caller's live arrivals could show up as the other's. Fine for a one-call-at-
a-time demo; a real multi-tenant version needs AssemblyAI to pass a session
id through to the tool call itself (or a per-phone-number/per-IP key) rather
than relying on "whichever call started most recently"."""

from __future__ import annotations

import json
import uuid
from datetime import datetime

# --- active call ------------------------------------------------------

_active_call_id: str | None = None


def start_call() -> str:
    global _active_call_id
    _active_call_id = uuid.uuid4().hex
    _events.clear()  # a new call starts with a clean event log, not the last caller's
    return _active_call_id


def active_call_id() -> str | None:
    return _active_call_id


# --- tool-call event log ---------------------------------------------------
# HTTP tools run on AssemblyAI's servers, so the browser never sees a
# tool.call event. Recording each hit here is what lets the demo page show
# them arriving.

_events: list[dict] = []


def _safe_json(raw: bytes) -> dict:
    try:
        return json.loads(raw or b"{}")
    except (ValueError, TypeError):
        return {}


def log_event(path: str, request_body: bytes, response_body: bytes) -> None:
    _events.append({
        "seq": len(_events) + 1,
        "call_id": _active_call_id,
        "tool": path.rsplit("/", 1)[-1],
        "arguments": _safe_json(request_body),
        "result": _safe_json(response_body),
        "at": datetime.now().strftime("%H:%M:%S"),
    })


def log_alert_event(message: str, category: str | None = None) -> None:
    _events.append({
        "seq": len(_events) + 1,
        "call_id": _active_call_id,
        "tool": "arrival_alert",
        "alert": True,
        "arguments": {},
        "result": {"ok": True, "message": message, "category": category},
        "at": datetime.now().strftime("%H:%M:%S"),
    })


def events_since(cursor: int, call_id: str | None) -> list[dict]:
    return [e for e in _events if e["seq"] > cursor and e["call_id"] == call_id]


# --- arrival alerts ----------------------------------------------------
# Registered by the set_arrival_alert tool, checked opportunistically every
# time gtfs.ensure_fresh() runs (tool calls and /api/events polling both
# trigger it). Fire once, then removed.

_alerts: list[dict] = []
_next_alert_id = 1


def add_alert(stop_name: str, stop_ids: list[str], route_id: str, route_short_name: str, threshold_minutes: int) -> int:
    global _next_alert_id
    alert_id = _next_alert_id
    _next_alert_id += 1
    _alerts.append({
        "id": alert_id,
        "stop_name": stop_name,
        "stop_ids": stop_ids,
        "route_id": route_id,
        "route_short_name": route_short_name,
        "threshold_minutes": threshold_minutes,
    })
    return alert_id


def check_alerts(get_arrivals) -> None:
    """get_arrivals(stop_ids, route_id) -> list of {eta_seconds, ...}."""
    if not _alerts:
        return
    fired = []
    for alert in _alerts:
        arrivals = get_arrivals(alert["stop_ids"], alert["route_id"])
        if not arrivals:
            continue
        best = min(arrivals, key=lambda a: a["eta_seconds"])
        if best["eta_seconds"] <= alert["threshold_minutes"] * 60:
            minutes = best["eta_seconds"] // 60
            message = (
                f"Route {alert['route_short_name']} is about {minutes} minute{'s' if minutes != 1 else ''} "
                f"from {alert['stop_name']}."
            )
            log_alert_event(message, best.get("category"))
            fired.append(alert)
    for alert in fired:
        _alerts.remove(alert)
