"""In-memory demo state: the tool-call event log and arrival alerts."""

from __future__ import annotations

import json
from datetime import datetime

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
        "tool": path.rsplit("/", 1)[-1],
        "arguments": _safe_json(request_body),
        "result": _safe_json(response_body),
        "at": datetime.now().strftime("%H:%M:%S"),
    })


def log_alert_event(message: str, category: str | None = None) -> None:
    _events.append({
        "seq": len(_events) + 1,
        "tool": "arrival_alert",
        "alert": True,
        "arguments": {},
        "result": {"ok": True, "message": message, "category": category},
        "at": datetime.now().strftime("%H:%M:%S"),
    })


def events_since(cursor: int) -> list[dict]:
    return [e for e in _events if e["seq"] > cursor]


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
