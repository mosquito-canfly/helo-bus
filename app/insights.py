"""Durable tool-call log for the /insights analytics page.

Separate from app/store.py's in-memory log (which only needs to survive one
page load, for the live "Live bus info" panel) — this persists to SQLite so
/insights has something to show across restarts. Render's disk is ephemeral,
so this resets on every deploy: fine for a hackathon demo, not a real
analytics pipeline.

Read-only with respect to the rest of the app: nothing here feeds back into
find_stop/next_arrivals/plan_trip or agent.json. It only watches tool calls
after main.py's middleware has already produced a response.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

from .gtfs import KL_TZ, STALE_CAVEAT

log = logging.getLogger("helo_buskl.insights")

DB_FILE = Path(__file__).resolve().parent.parent / "data" / "insights.db"


def _connect() -> sqlite3.Connection:
    DB_FILE.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_FILE)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            at REAL NOT NULL,
            tool TEXT NOT NULL,
            arguments TEXT NOT NULL,
            result TEXT NOT NULL,
            is_sample INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    return conn


def log_event(path: str, request_body: bytes, response_body: bytes) -> None:
    """Best-effort: a DB hiccup must never break the tool call itself."""
    try:
        tool = path.rsplit("/", 1)[-1]
        args = json.loads(request_body or b"{}")
        result = json.loads(response_body or b"{}")
        with _connect() as conn:
            conn.execute(
                "INSERT INTO events (at, tool, arguments, result, is_sample) VALUES (?, ?, ?, ?, 0)",
                (time.time(), tool, json.dumps(args), json.dumps(result)),
            )
    except Exception:
        log.exception("failed to persist tool-call event (tool call itself is unaffected)")


def seed(rows: list[tuple[float, str, dict, dict]]) -> None:
    """Bulk-insert sample rows, marked is_sample=1. Only ever called by
    scripts/seed_insights.py, and only meant to run against an empty table —
    see that script for why."""
    with _connect() as conn:
        conn.executemany(
            "INSERT INTO events (at, tool, arguments, result, is_sample) VALUES (?, ?, ?, ?, 1)",
            [(at, tool, json.dumps(args), json.dumps(result)) for at, tool, args, result in rows],
        )


def _rows() -> list[dict]:
    with _connect() as conn:
        cur = conn.execute("SELECT at, tool, arguments, result, is_sample FROM events ORDER BY at")
        out = []
        for at, tool, args_json, result_json, is_sample in cur.fetchall():
            out.append({
                "at": at,
                "tool": tool,
                "arguments": json.loads(args_json),
                "result": json.loads(result_json),
                "is_sample": bool(is_sample),
            })
        return out


def _freshness(reason: str | None, ok: bool, message: str) -> str | None:
    """None means this event doesn't say anything about feed freshness
    (e.g. find_stop, or a resolution failure that never got as far as
    checking live data)."""
    if reason == "feed_unavailable":
        return "unavailable"
    if STALE_CAVEAT in message:
        return "stale"
    if ok or reason == "no_buses_nearby":
        return "fresh"
    return None


def summary() -> dict:
    """Everything /insights shows, computed fresh from the DB each request —
    this is a demo-scale dataset (hundreds to low thousands of rows), so a
    Python pass over all rows is simpler and plenty fast; no need for SQL
    aggregation until that stops being true."""
    rows = _rows()
    is_sample = bool(rows) and all(r["is_sample"] for r in rows)

    stop_counts: Counter[str] = Counter()
    route_counts: Counter[str] = Counter()
    unmet_demand: Counter[tuple[str, str]] = Counter()
    unresolved: Counter[str] = Counter()
    by_hour: Counter[int] = Counter()
    freshness_counts: Counter[str] = Counter()

    for row in rows:
        tool, args, result = row["tool"], row["arguments"], row["result"]
        ok = bool(result.get("ok"))
        reason = result.get("reason")
        message = result.get("message", "")

        by_hour[datetime.fromtimestamp(row["at"], tz=KL_TZ).hour] += 1

        if tool == "find_stop":
            if ok and result.get("stop"):
                stop_counts[result["stop"]] += 1
            elif reason == "not_found" and args.get("query"):
                unresolved[args["query"]] += 1

        elif tool == "next_arrivals":
            if ok and result.get("stop"):
                stop_counts[result["stop"]] += 1
            elif reason == "not_found" and args.get("stop"):
                unresolved[args["stop"]] += 1
            if args.get("route"):
                route_counts[args["route"]] += 1
            fresh = _freshness(reason, ok, message)
            if fresh:
                freshness_counts[fresh] += 1

        elif tool == "plan_trip":
            if ok:
                if result.get("from_stop"):
                    stop_counts[result["from_stop"]] += 1
                if result.get("to_stop"):
                    stop_counts[result["to_stop"]] += 1
            elif reason == "not_found":
                for key in ("from_stop", "to_stop"):
                    if args.get(key):
                        unresolved[args[key]] += 1
            elif reason == "no_direct_route" and args.get("from_stop") and args.get("to_stop"):
                unmet_demand[(args["from_stop"], args["to_stop"])] += 1
            fresh = _freshness(reason, ok, message)
            if fresh:
                freshness_counts[fresh] += 1

    total_fresh_checks = sum(freshness_counts.values())
    freshness_pct = (
        {k: round(100 * v / total_fresh_checks, 1) for k, v in freshness_counts.items()}
        if total_fresh_checks
        else {}
    )

    return {
        "is_sample": is_sample,
        "total_calls": len(rows),
        "top_stops": stop_counts.most_common(10),
        "top_routes": route_counts.most_common(10),
        "unmet_demand": [
            {"from": f, "to": t, "count": c} for (f, t), c in unmet_demand.most_common(10)
        ],
        "unresolved_names": unresolved.most_common(10),
        "by_hour": [by_hour.get(h, 0) for h in range(24)],
        "freshness_pct": freshness_pct,
    }
