"""Rank RapidKL stop names and route codes by significance, merge into
agent.json's keyterms so AssemblyAI's STT is biased toward hearing them
correctly.

AssemblyAI caps keyterms at 100 per session, 50 characters each. Stop names
come first (that's the actual reported problem — mishearing Malay place
names), routes fill whatever budget is left; with ~140 routes in this feed
there isn't room for "all" of them alongside 80 stops, so the busiest ones
(by stops served) win.

Run after any GTFS refresh, or whenever agent.json's hand-written keyterms
change: python scripts/build_keyterms.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))  # so `app` resolves when run as a script, not a module

from app import gtfs  # noqa: E402

AGENT_JSON = ROOT / "agent.json"

TOTAL_CAP = 100  # AssemblyAI's hard limit
MAX_LEN = 50  # AssemblyAI's per-keyterm length limit
STOP_LIMIT = 80

# Hand-written keyterms to always keep, independent of the GTFS ranking.
# Fixed, not "whatever's currently in agent.json" — reading the live file
# would make every rerun treat the previous run's own output as sacred
# "existing" content, snowballing the stop/route split with each rerun.
BASE_KEYTERMS = ["RapidKL", "KL Sentral", "Pasar Seni", "Mid Valley", "next bus", "arrival", "platform"]


def _dedup_case_insensitive(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out = []
    for item in items:
        key = item.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def main() -> None:
    definition = json.loads(AGENT_JSON.read_text(encoding="utf-8"))

    stop_names = [n for n in gtfs.top_stop_names(STOP_LIMIT) if len(n) <= MAX_LEN]
    route_names = [n for n in gtfs.top_route_short_names() if len(n) <= MAX_LEN]

    combined = _dedup_case_insensitive(BASE_KEYTERMS + stop_names + route_names)
    routes_included = max(0, min(len(route_names), TOTAL_CAP - len(_dedup_case_insensitive(BASE_KEYTERMS + stop_names))))
    keyterms = combined[:TOTAL_CAP]

    definition["keyterms"] = keyterms
    AGENT_JSON.write_text(json.dumps(definition, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    print(f"stop names ranked:   {len(stop_names)} (top {STOP_LIMIT} by routes served)")
    print(f"route codes ranked:  {len(route_names)} available, {routes_included} fit in the remaining budget")
    print(f"keyterms written:    {len(keyterms)} / {TOTAL_CAP} to {AGENT_JSON}")
    if len(route_names) > routes_included:
        print(f"skipped {len(route_names) - routes_included} lower-traffic routes — over the 100-keyterm cap.")


if __name__ == "__main__":
    main()
