# Helo Bus

A voice assistant that tells RapidKL bus riders in Kuala Lumpur when their
bus is arriving, built on AssemblyAI's Voice Agent API using **server-side
HTTP tools** — so there is no tool dispatcher and nothing of yours stays
connected during a call.

Built for the [AssemblyAI Voice Agent Hackathon](https://lablab.ai/ai-hackathons/assemblyai-voice-agent-hackathon)
on lablab.ai. Started from [vicdevman/assemblyai-voice-agent-scheduler](https://github.com/vicdevman/assemblyai-voice-agent-scheduler),
the reference implementation for the tutorial *"Ship a Voice Agent That
Books Appointments Without Writing a Tool Dispatcher"* — same HTTP-tools
pattern, new domain: live bus arrivals instead of dental bookings.

## The idea

Most voice-agent tutorials put your program in the middle of every call:

```
caller <-> AssemblyAI <-> your script (connected all call) <-> your logic
                          listens for tool.call, replies with tool.result
```

This one doesn't. You describe the agent once as JSON, hand AssemblyAI a set of
URLs, and it calls your API itself:

```
caller <-> AssemblyAI --HTTP POST--> your bus arrival API --> RapidKL's live GTFS feed
```

Close your laptop and the agent still answers.

## 1. Install and run the API

```bash
python -m venv .venv
.venv/Scripts/activate          # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt

uvicorn app.main:app --reload   # http://localhost:8000
```

Check it answers:

```bash
curl -X POST http://localhost:8000/tools/get_now
```

GTFS data itself is precomputed, not fetched at startup — `data/static.json`
ships committed, so a fresh clone runs immediately. Refresh it after a GTFS
update with `python scripts/build_static.py` (downloads both categories,
parses them once, writes the compact file back).

## 2. Put the API on the public internet

**This is the step people get stuck on.** AssemblyAI calls your tools from its
own servers, so `http://localhost:8000` is unreachable to it — and the publish
script rejects it rather than letting you find out mid-call. You need a public
**HTTPS** URL. Two easy ways:

### Option A — ngrok

```bash
ngrok http 8000
```

It prints a forwarding line. Copy the **https** one:

```
Forwarding   https://a1b2-102-89-33-14.ngrok-free.app -> http://localhost:8000
```

Your value is `https://a1b2-102-89-33-14.ngrok-free.app`

### Option B — Cloudflare Tunnel

No account needed for a quick tunnel:

```bash
cloudflared tunnel --url http://localhost:8000
```

It prints a URL like:

```
https://formal-tribune-serving-mathematics.trycloudflare.com
```

Your value is `https://formal-tribune-serving-mathematics.trycloudflare.com`

### Either way

Put it in `.env` with **no trailing slash**:

```bash
PUBLIC_API_BASE_URL=https://a1b2-102-89-33-14.ngrok-free.app
```

Confirm the outside world can actually reach it before going further:

```bash
curl -X POST https://a1b2-102-89-33-14.ngrok-free.app/tools/get_now
```

If that returns the current time in Kuala Lumpur, AssemblyAI can reach it too.

> **These URLs change.** Both free tiers hand you a new address every restart.
> When yours changes, update `.env` and re-run the publish script with
> `--update <agent_id>` — otherwise the agent keeps calling a dead URL and every
> tool times out mid-conversation.

## 3. Publish the agent

Copy `.env.example` to `.env` and fill in `ASSEMBLYAI_API_KEY`
([free account, $50 of non-expiring credit](https://www.assemblyai.com/dashboard)),
then:

```bash
python scripts/create_agent.py
```

It checks your public URL is live, uploads `agent.json`, and prints an
`agent_id` (also written to `agent_id.txt`). After editing `agent.json`:

```bash
python scripts/create_agent.py --update <agent_id>
```

## 4. Talk to it

Open <http://localhost:8000> and press **Start call**. Ask when the next bus
is at KL Sentral.

The left pane is the conversation. The right pane fills with the HTTP calls
AssemblyAI makes to your bus arrival API while you talk — no code in the
browser handles them, it is reading your API's own log. Each agent reply
carries chips naming the tool calls behind it; click one to jump to it. If
you set an arrival alert, a banner (with a short beep) appears across the top
once the bus you're waiting for crosses your threshold.

Your API key never reaches the browser: `GET /api/token` mints a 5-minute token
server-side and the socket authenticates with that.

## Layout

| Path | What it is |
| --- | --- |
| `agent.json` | The whole agent: prompt, voice, keyterms, 4 HTTP tools |
| `app/main.py` | Bus arrival API — the endpoints AssemblyAI calls |
| `app/gtfs.py` | GTFS static + live data: stop search, ETA computation, alerts |
| `app/store.py` | The demo event log and registered arrival alerts |
| `scripts/create_agent.py` | Publishes `agent.json` to `/v1/agents` |
| `web/` | The demo page: mic capture, transcript, live tool feed, alert banner |

## The four tools

| Tool | Why it exists |
| --- | --- |
| `get_now` | The model has no clock. Without this it guesses the time, and guesses wrong. |
| `find_stop` | Resolves a spoken stop name or landmark to an exact RapidKL stop, asking to disambiguate when several stops share a name. |
| `next_arrivals` | Live ETAs for a stop, one route or every route serving it. |
| `set_arrival_alert` | Registers a one-shot alert that fires once a named route is within N minutes of a stop. |

Every response carries an `ok` flag and a `message` written to be read aloud.
Failures are values, not exceptions — an ambiguous stop name comes back with
the candidates still listed, so the agent recovers inside the conversation
instead of apologising and hanging up. `next_arrivals` distinguishes three
different reasons it might come back empty: RapidKL isn't running at this
hour, the live feed is temporarily unreachable, or the service is running but
nothing is close enough right now — each gets its own spoken message so the
agent never claims buses have stopped running when the feed just blipped.

## Where the data comes from

Static schedule (stops, routes, shapes) and live vehicle positions both come
from [data.gov.my's Transport API](https://developer.data.gov.my/realtime-api/gtfs-static),
Prasarana's `rapid-bus-kl` category — the same public GTFS feed a teammate's
`where-bus` project (Next.js + Spring Boot) also builds on. This repo is an
independent Python port, not a fork: nearest-stop GPS snapping onto a
cumulative-distance table built from each route's shape polyline, direction
handling, and the "already passed" filter all follow the same approach
validated there, ported from Java. `where-bus` itself is not called or
depended on at runtime.

## Two layers of validation, and which to use

The tool schemas use `pattern`, `enum` and `examples`. Values failing those are
rejected *before* your API is called and the agent re-asks. That is the right
place for anything the agent can fix by listening again.

It is the wrong place for anything the agent needs *explained*. If a caller
names a stop two different platforms share (a real RapidKL quirk — Pasar Seni
alone has ten), rejecting the input teaches the agent nothing about what to
ask next. Instead `find_stop` and `next_arrivals` answer with something
speakable:

```json
{ "ok": false,
  "reason": "ambiguous_stop",
  "candidates": ["Pasar Seni (Platform A6 - A8)", "Pasar Seni (Platform B3 - B4)", "..."],
  "message": "There's more than one stop with that name: ... Which one?" }
```

Now the agent asks the right question and the call completes. **Schema for what
the agent can fix by re-asking; your API for what it needs explained.**

## Deploying it

A tunnel is fine while you build, but the URL dies with your terminal. To put
this somewhere permanent, note one thing first: **the event log and pending
alerts live in memory**, so this wants a long-running process, not a
serverless function.

On Render, Railway or Fly it deploys as-is:

```
Build:  pip install -r requirements.txt
Start:  uvicorn app.main:app --host 0.0.0.0 --port $PORT
```

Set `ASSEMBLYAI_API_KEY` in the host's environment, then point the agent at the
new address and re-publish:

```bash
PUBLIC_API_BASE_URL=https://your-app.onrender.com
python scripts/create_agent.py --update <agent_id>
```

Vercel supports FastAPI too, but its functions are ephemeral and can run on
different instances per request. The browser polls `/api/events` several times
a second, so it would keep missing events that landed elsewhere — and each
cold start would re-download the GTFS cache. A long-running host is the
better fit here.

You never need server-side WebSockets, whatever you choose. The browser talks
straight to AssemblyAI; your API only ever answers plain HTTP.

## Where to take it next

This is a starting point, not a finished product. Obvious directions:

- **Add MRT Feeder.** `rapid-bus-kl` is the only GTFS category loaded; the
  same loader run against `rapid-bus-mrtfeeder` covers the rest of the fleet.
- **Persist alerts.** They currently live in memory and don't survive a
  restart — fine for a demo call, not for a real overnight wait.
- **Send a real push notification.** The browser banner and beep stand in for
  an SMS or push alert a caller could receive after hanging up.
- **Put it on a phone number.** Deliberately left out here: Twilio trial
  numbers only dial numbers you have verified in advance, so nobody else
  could ring it. Once you are on a paid number, AssemblyAI connects over SIP.

## Licence

MIT — see [LICENSE](LICENSE). Built on vicdevman's tutorial scaffold; see
that repo for the original booking-agent version this one replaced.
