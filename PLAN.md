# RapidKL Voice Agent — Plan

Turns the Brightsmile dental-scheduler tutorial into a voice assistant for
RapidKL bus riders, built on the same pattern: AssemblyAI calls our FastAPI
tools directly over HTTP, nothing of ours stays connected during a call.

`../where-bus` (Next.js + Spring Boot, teammate-owned) is read-only reference
for the GTFS pipeline and ETA math — never modified, copied from, or called.

## where-bus reference: data pipeline and ETA logic

- **`TransitService.java`** — on startup, loads GTFS static CSVs
  (`stops.txt`, `routes.txt`, `trips.txt`, `stop_times.txt`, `shapes.txt`) for
  two categories (`rapid-bus-kl`, `rapid-bus-mrtfeeder`) into HashMaps:
  `stopDirectory` (id→stop), `routeDirectory` (id→route),
  `shortNameToRouteId` (public "T815" → internal GTFS id), `routePaths`
  (ordered stop-id list per `"routeId_directionId"`, taken from one
  representative trip per route-direction), and `stopCumulativeDistances` —
  cumulative road-distance in km from route start to each stop, parallel to
  `routePaths`. That table comes straight from `shape_dist_traveled` when the
  feed provides it (mrtfeeder), or is computed by summing haversine distances
  along the shape polyline when it doesn't (rapid-bus-kl). Also builds an
  inverted index: stop_id → routes serving it.
- **`LiveTrackingService.java`** — polls the two GTFS-RT vehicle-position
  protobuf feeds every 30s, staggered 10s apart (data.gov.my allows 4 req/min
  total), with 429 backoff and "keep the last known fleet rather than wipe it"
  during a blackout. Confirms: **positions only, no trip-updates feed** — ETAs
  are derived, not read off a schedule. Handles route-id broadcast quirks
  (trailing `"0"`, `"T155 Outbound"` suffix).
- **`EtaCalculationService.java`** — for each active vehicle on the route:
  snaps the bus's GPS to its **nearest stop index** (not true polyline
  projection — their own docs call this acceptable at 30s refresh) to read its
  cumulative distance; `ETA = (stopDist − busDist) / speed_mps`; speed from
  the feed (km/h→m/s) or an ~11km/h fallback; buses whose distance already ≥
  the stop's are dropped (the "already passed" / direction filter — same
  cumulative-distance table, no separate geometry needed); a 3-sample rolling
  average smooths GPS jitter between polls; results >35 min are dropped;
  "Arriving" shown under 150m. Falls back to haversine×1.4 when shape data is
  missing (and can't filter "passed" in that fallback case).
- **Known limitations** (from their README, taken as precedent for what's
  acceptable to cut here too): nearest-stop snapping instead of true polyline
  projection, no traffic/dwell accounting, static data never live-refreshed,
  circular routes can break the passed-filter.

## Scope decisions

- **`rapid-bus-kl` only.** Confirmed as the correct static category — the
  where-bus data folder is literally named that, matching the RT category
  param. MRT Feeder is a copy-paste of the same loader later if wanted, not
  now.
- **Two files.** `app/store.py` keeps its existing job (alerts + demo event
  log — small, already fits the repo's flat-function style). New
  `app/gtfs.py` owns the transit data layer: static load, live poll/cache,
  ETA computation, fuzzy stop search. Mixing CSV parsing + distance math into
  `store.py` would bloat one file for two unrelated concerns.
- **One new dependency:** `gtfs-realtime-bindings` (protobuf parsing for the
  RT feed — hand-rolling that parser would be worse than one pip install).
  Everything else — zip/CSV/haversine/fuzzy-match/timezone — is stdlib, plus
  `httpx` already in `requirements.txt`.

## Static GTFS load (`app/gtfs.py`, at import — same pattern as store.py's
`_seed_existing_bookings()`)

- **Disk cache, gitignored.** On import: if `.gtfs_cache/rapid-bus-kl/*.txt`
  exists on disk, load from there — no network call, no startup dependency on
  data.gov.my being fast or up. If missing, `httpx.get(".../gtfs-static/
  prasarana?category=rapid-bus-kl")`, unzip into that folder, then load.
  A slow or down data.gov.my only blocks the *first ever* run, never a normal
  restart. `.gtfs_cache/` added to `.gitignore`.
- Parse with stdlib `csv.DictReader` (files are small; no pandas).
- Build: `stops`, `routes`, `short_name_to_route_id`, `route_paths`
  (`"routeId_dir"` → ordered stop-id list), `stop_cum_dist` (parallel
  cumulative-km list, always computed via summed haversine along the shape
  polyline — rapid-bus-kl has no `shape_dist_traveled`, so the
  column-presence branch where-bus needs for the other feed is skipped
  entirely).
- `stop_to_routes` inverted index (stop_id → serving route/direction pairs),
  same purpose as where-bus's `getRoutesForStop` — lets `next_arrivals`
  find candidate routes without the caller naming one.

### Stop-name grouping and aliases

RapidKL has many same-named stops (opposite sides of a road, different
routes' platforms at one interchange). `find_stop`:

- Normalises GTFS stop names before matching: lowercase, strip punctuation,
  collapse common abbreviations (`"Jln"` → `"Jalan"`, `"Psn"` → `"Persiaran"`,
  `"Lrg"` → `"Lorong"`, etc. — small fixed dict, not a general NLP pass).
- Groups stops sharing a normalised name into one `StopGroup` (list of
  stop_ids + shared display name), built once at load time alongside the
  other indexes.
- A small hand-written alias map for common spoken names that don't literally
  match GTFS text — `"KL Sentral"`, `"Pasar Seni"`, `"Mid Valley"`, `"UM"` →
  their actual stop-group name(s). Checked before falling back to fuzzy
  match.
- Fuzzy match itself: stdlib `difflib.get_close_matches` over normalised
  group names — good enough for spoken-name matching against a few thousand
  stops; skip adding rapidfuzz/thefuzz.

`find_stop` returns one or more `StopGroup` candidates (top matches if
ambiguous). `next_arrivals` accepts a stop group (all its member stop_ids),
merges arrivals across every stop in the group by default. If the group's
member stops genuinely serve different directions/destinations for the same
route (i.e. merging would misinform which way to walk), it returns
`ok:false` asking which direction/destination the caller means, instead of
silently merging.

## Live polling/cache

- No background scheduler thread. Module-level `_vehicles` dict +
  `_last_fetch` timestamp. `_ensure_fresh()` refetches only if stale (>30s),
  called at the top of any code path that needs live data.
- Parsed via `gtfs-realtime-bindings`' `google.transit.gtfs_realtime_pb2`.
- On fetch failure: keep the last-known cache (same blackout behavior as
  where-bus). Skipping their explicit 429/Retry-After backoff bookkeeping —
  on-demand fetch triggered by tool calls / event polling is naturally far
  below the 4 req/min limit, unlike their continuous 30s scheduler.
  `# ponytail:` comment marks this ceiling — add real backoff bookkeeping if
  this ever runs as a standing service with many concurrent callers instead
  of one demo call at a time.
- **`GET /api/events` also drives `_ensure_fresh()` and the alert check**
  (still gated at max once per 30s — the browser's existing ~400ms poll loop
  doesn't turn into 400ms-frequency fetches). This is what makes alerts fire
  without a tool call in flight: the demo page is already polling
  `/api/events` every 400ms per `web/app.js`, so hanging the refresh off that
  poll reuses transport that already exists instead of adding a second
  timer/thread.

## ETA computation

Same nearest-stop-snap projection and cumulative-distance subtraction as
`EtaCalculationService` — reusing where-bus's own judgment that true polyline
projection isn't worth it at 30s refresh. Drop "passed" buses via the same
cumulative-distance check, drop results >35 min out, "Arriving" under
~150m/60s, sort ascending, cap to a handful for a spoken list ("T789 in 4
minutes, then 12 minutes").

### Empty-result cases (three distinct spoken messages)

1. **Night — no service.** Current time (Asia/Kuala_Lumpur) is outside
   RapidKL's operating hours (~6am–11pm) and no vehicles are active at all.
   Message explains service isn't running now and gives the next start time.
2. **Feed unavailable / stale.** It's within operating hours but the live
   feed hasn't returned fresh data (fetch failing, or `_last_fetch` is old
   with nothing cached yet). Message says live positions aren't available
   right now and suggests trying again shortly — doesn't claim buses have
   stopped running.
3. **Running, but nothing within range.** Vehicles are active and the feed is
   fresh, but nothing on the relevant route(s) is within the 35-minute
   window for this stop. Message says no buses are currently close enough,
   distinct from both of the above.

Each case maps to its own `ok:false` + `reason` + spoken `message`, same
`{ok, message}` contract the dental tutorial already uses throughout
`app/main.py`.

## Timezone

`Asia/Kuala_Lumpur`, no DST, used explicitly everywhere time matters —
`get_now`, the night-service check, alert timestamps. `zoneinfo` is stdlib
but ships with **no tz database on Windows**, so `tzdata` is added to
`requirements.txt` (the stdlib-recommended companion package) rather than
reaching for `pytz`.

## Tools (replacing the dental tutorial's four)

- **`get_now`** — current KL date, time, and weekday, spoken. Also the thing
  the agent calls before reasoning about "is it running now" / relative
  times, same role `get_today` played for the dental agent.
- **`find_stop(query)`** — normalise, check alias map, then fuzzy-match
  against grouped stop names. Returns top candidate group(s); the agent reads
  them back if more than one is plausible.
- **`next_arrivals(stop_group, route?)`** — resolve serving routes via
  `stop_to_routes` (or narrow to `route` if the caller named one), refresh
  live data if stale, compute ETAs, merge across the group's member stops,
  handle the direction-ambiguous and three empty-result cases above.
- **`set_arrival_alert(stop_group, route, threshold_minutes)`** — registers
  `{stop_ids, route, threshold}` in the same in-memory list/event-log
  mechanism `store.py` already has. Checked opportunistically inside the
  `_ensure_fresh()` path (tool calls and `/api/events` polling both trigger
  it), so no separate scheduler. On threshold crossing, pushes a synthetic
  event into the existing log.

## Demo page changes

- `web/app.js`'s existing `/api/events` poll loop now also carries alert
  events. When one arrives: show a clear banner (reusing the existing
  card/chip rendering pattern) and play a short sound (a tiny inline
  `AudioContext` beep — no audio asset file needed, one less thing to ship
  and license).
- `agent.json` rewritten: name, greeting, system prompt, and `keyterms`
  seeded with common KL stop names (from the alias map) so the transcriber
  hears them correctly.
- New `.gitignore` entry for `.gtfs_cache/`.
- `LICENSE` (MIT) and a `README.md` skeleton for the new project identity.

## Explicitly not doing (say so, don't build it)

- No MRT Feeder feed — one category is enough to demo the mechanism; adding
  the second is the same loader run twice.
- No background scheduler/thread for live polling — refresh-on-access covers
  freshness at this call volume.
- No true polyline projection for bus position — nearest-stop snapping is the
  precedent where-bus itself settled on.
- No 429/backoff bookkeeping for the RT feed — on-demand fetch volume doesn't
  approach the rate limit the way a continuous scheduler does.
- No external fuzzy-match dependency — `difflib` is enough for stop-name
  matching at this scale.
- No audio asset for the alert sound — a generated beep is smaller and
  simpler than shipping/licensing a file.
