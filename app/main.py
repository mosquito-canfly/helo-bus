"""Bus arrival API for the Helo Bus voice agent.

AssemblyAI's Voice Agent API calls these endpoints directly as HTTP tools.
Every response is shaped for a language model to read aloud: short, literal,
and explicit about failure so the agent can recover mid-call instead of
inventing an answer.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.responses import Response

from . import gtfs, store

load_dotenv(Path(__file__).resolve().parent.parent / ".env")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
log = logging.getLogger("helo_bus.main")

app = FastAPI(title="Helo Bus", version="1.0.0")
ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = ROOT / "web"


@app.middleware("http")
async def record_tool_calls(request, call_next):
    """Log every tool hit so the demo page can show AssemblyAI calling us —
    and how long it took, since a tool call that's slow enough to hit
    AssemblyAI's own timeout never gets this far to log its response."""
    if not request.url.path.startswith("/tools/"):
        return await call_next(request)
    start = time.time()
    body = await request.body()
    response = await call_next(request)
    elapsed = time.time() - start
    log.info("tool call %s: %.2fs", request.url.path, elapsed)
    payload = b"".join([chunk async for chunk in response.body_iterator])
    store.log_event(request.url.path, body, payload)
    return Response(
        content=payload,
        status_code=response.status_code,
        headers={k: v for k, v in response.headers.items() if k.lower() != "content-length"},
        media_type=response.media_type,
    )


class FindStopRequest(BaseModel):
    query: str = Field(description="A stop name or landmark as the caller said it, e.g. 'KL Sentral'")


class NextArrivalsRequest(BaseModel):
    stop: str = Field(description="Stop name, as returned by find_stop or as the caller said it")
    route: str | None = Field(default=None, description="Route short name, e.g. 'T789' or '300'. Omit for all routes.")


class PlanTripRequest(BaseModel):
    from_stop: str = Field(description="Stop name the caller is starting from, as they said it")
    to_stop: str = Field(description="Stop name the caller wants to reach, as they said it")


class SetAlertRequest(BaseModel):
    stop: str = Field(description="Stop name, as returned by find_stop or as the caller said it")
    route: str = Field(description="Route short name, e.g. 'T789' or '300'")
    threshold_minutes: int = Field(description="Notify when the bus is about this many minutes away", ge=1, le=35)


class LocationRequest(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/tools/get_now")
def get_now() -> dict:
    """The model has no clock. Without this it guesses the time, and guesses wrong."""
    return gtfs.get_now()


@app.post("/tools/find_stop")
def find_stop(req: FindStopRequest) -> dict:
    return gtfs.find_stop(req.query)


@app.post("/tools/next_arrivals")
def next_arrivals(req: NextArrivalsRequest) -> dict:
    return gtfs.next_arrivals(req.stop, req.route)


@app.post("/tools/plan_trip")
def plan_trip(req: PlanTripRequest) -> dict:
    return gtfs.plan_trip(req.from_stop, req.to_stop)


@app.post("/tools/set_arrival_alert")
def set_arrival_alert(req: SetAlertRequest) -> dict:
    return gtfs.set_arrival_alert(req.stop, req.route, req.threshold_minutes)


@app.post("/tools/find_nearby_stops")
def find_nearby_stops() -> dict:
    return gtfs.find_nearby_stops()


@app.post("/api/location")
def api_location(req: LocationRequest) -> dict:
    """The browser posts geolocation here (with the caller's permission) so
    find_nearby_stops has something to search from — the agent itself never
    receives raw coordinates, only stop names and walking distances."""
    gtfs.set_location(req.lat, req.lon)
    return {"ok": True}


# --- demo endpoints -------------------------------------------------------


@app.get("/api/events")
def api_events(since: int = 0) -> dict:
    gtfs.ensure_fresh()  # also checks alert thresholds; gated to once/30s internally
    events = store.events_since(since)
    return {"events": events, "cursor": events[-1]["seq"] if events else since}


@app.get("/api/config")
def api_config() -> dict:
    agent_id_file = ROOT / "agent_id.txt"
    agent_id = os.getenv("AGENT_ID") or (
        agent_id_file.read_text(encoding="utf-8").strip() if agent_id_file.exists() else ""
    )
    return {"agent_id": agent_id}


@app.get("/api/demo-info")
def api_demo_info() -> dict:
    """Everything the agent knows, split by where it comes from.

    agent.json is uploaded once and never changes during a call. The transit
    network below is read live, on every single tool call.
    """
    definition = json.loads((ROOT / "agent.json").read_text(encoding="utf-8"))
    summary = gtfs.network_summary()

    return {
        "agent": {
            "name": definition["name"],
            "voice": definition["voice"]["voice_id"],
            "keyterms": definition.get("keyterms", []),
            "tools": [
                {"name": t["name"], "url": t["http"]["url"].rsplit("/", 1)[-1]}
                for t in definition.get("tools", [])
            ],
        },
        "network": summary,
    }


@app.get("/api/token")
def api_token() -> dict:
    """Mint a short-lived token. The browser must never see the API key."""
    api_key = os.getenv("ASSEMBLYAI_API_KEY")
    if not api_key:
        raise HTTPException(500, "ASSEMBLYAI_API_KEY is not set on the server")
    resp = httpx.get(
        "https://agents.assemblyai.com/v1/token",
        headers={"Authorization": f"Bearer {api_key}"},
        params={"expires_in_seconds": 300, "max_session_duration_seconds": 600},
        timeout=15,
    )
    if resp.status_code >= 400:
        raise HTTPException(resp.status_code, f"Token request failed: {resp.text}")
    return {"token": resp.json()["token"]}


if WEB_DIR.exists():
    app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")
