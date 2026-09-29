const SAMPLE_RATE = 24000;
const WS_URL = "wss://agents.assemblyai.com/v1/ws";
const POLL_MS = 400;
const MAP_POLL_MS = 3000; // vehicle positions only change ~every 45s server-side; no need for transcript-speed polling

if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("/sw.js").catch(() => {}); // best-effort; app works fine without it
}

// -------------------------------------------------------------- install

// navigator.standalone is iOS's own flag for "already installed", distinct
// from the standard display-mode media query — checked either way since
// this app is already running installed either way once true.
const isIOS = /iPad|iPhone|iPod/.test(navigator.userAgent);
const isStandalone = window.matchMedia("(display-mode: standalone)").matches || navigator.standalone === true;

const installBtn = document.getElementById("install-btn");
let deferredInstallPrompt = null;

// Android/Chrome offers an install prompt we can trigger ourselves; only
// show the button once the browser has actually offered one, and never
// while already running installed (a stray/late event shouldn't resurface it).
window.addEventListener("beforeinstallprompt", (e) => {
  e.preventDefault();
  deferredInstallPrompt = e;
  if (!isStandalone) installBtn.hidden = false;
});

installBtn.addEventListener("click", async () => {
  if (!deferredInstallPrompt) return;
  installBtn.hidden = true;
  deferredInstallPrompt.prompt();
  await deferredInstallPrompt.userChoice;
  deferredInstallPrompt = null;
});

window.addEventListener("appinstalled", () => {
  installBtn.hidden = true;
});

// iOS Safari has no beforeinstallprompt at all — the only way to install is
// Share -> Add to Home Screen, so just say that, once, and only if this
// isn't already running installed — dismissible, since it's a hint, not a
// permission prompt the user is forced to act on.
const iosInstallHint = document.getElementById("ios-install-hint");
if (isIOS && !isStandalone) {
  iosInstallHint.hidden = false;
}
document.getElementById("ios-install-hint-dismiss").addEventListener("click", () => {
  iosInstallHint.hidden = true;
});

const els = {
  talk: document.getElementById("talk"),
  status: document.getElementById("status"),
  transcript: document.getElementById("transcript"),
  calls: document.getElementById("calls"),
  timer: document.getElementById("timer"),
  alertBanner: document.getElementById("alert-banner"),
  alertText: document.getElementById("alert-text"),
  listening: document.getElementById("listening"),
};

let ws = null;
let audioCtx = null;
let micStream = null;
let workletNode = null;
let live = false;

// No text input to a voice agent, so an example prompt can't fake what the
// caller "said" — tapping one starts the call and leaves a note of what to
// try, once, instead of pretending a transcript line that never happened.
let currentAgentId = "";
let pendingPromptHint = null;

// Minted fresh by /api/call/start every time Start call is pressed, then
// carried on every /api/location, /api/map-state and /api/events request —
// see app/store.py's module docstring for why the server needs this (its
// tool calls, run by AssemblyAI, never carry an id of their own) and what
// it doesn't cover (two truly simultaneous callers).
let currentCallId = null;

// Playback scheduling. Holding the sources lets barge-in cut the agent off.
let playHead = 0;
let scheduled = [];

// Tool calls arrive out-of-band from the bus arrival API's own log. Buffer them so
// the next agent reply can show which ones fed it.
let eventCursor = 0;
let pollTimer = null;
let pending = [];
let lastLiveEvent = null; // { kind: "arrival" | "trip", result } — drives the right panel + map

// ---------------------------------------------------------------- helpers

function setStatus(text, state) {
  els.status.textContent = text;
  els.status.dataset.state = state;
}

// ------------------------------------------------------------------ timer

let timerStart = 0;
let timerTick = null;

function formatDuration(ms) {
  const total = Math.floor(ms / 1000);
  const minutes = Math.floor(total / 60);
  return `${minutes}:${String(total % 60).padStart(2, "0")}`;
}

function resetTimer() {
  clearInterval(timerTick);
  timerTick = null;
  timerStart = 0;
  els.timer.textContent = "0:00";
  els.timer.dataset.state = "idle";
}

function startTimer() {
  timerStart = Date.now();
  els.timer.textContent = "0:00";
  els.timer.dataset.state = "running";
  timerTick = setInterval(() => {
    els.timer.textContent = formatDuration(Date.now() - timerStart);
  }, 250);
}

function stopTimer() {
  if (!timerTick) return; // never connected, so nothing to hold
  clearInterval(timerTick);
  timerTick = null;
  els.timer.textContent = formatDuration(Date.now() - timerStart);
  els.timer.dataset.state = "ended";
}

function toBase64(buffer) {
  const bytes = new Uint8Array(buffer);
  let binary = "";
  const CHUNK = 0x8000;
  for (let i = 0; i < bytes.length; i += CHUNK) {
    binary += String.fromCharCode.apply(null, bytes.subarray(i, i + CHUNK));
  }
  return btoa(binary);
}

function fromBase64(b64) {
  const binary = atob(b64);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  return new Int16Array(bytes.buffer);
}

function clearEmpty(node) {
  const empty = node.querySelector(".empty");
  if (empty) empty.remove();
}

// ------------------------------------------------------------ transcript

function addLine(who, text) {
  clearEmpty(els.transcript);

  const row = document.createElement("div");
  row.className = `line ${who}`;

  const label = document.createElement("span");
  label.className = "who";
  label.textContent = who === "agent" ? "Agent" : "You";

  const body = document.createElement("p");
  body.textContent = text;
  row.append(label, body);

  // The visual payoff: a next_arrivals or plan_trip result behind this reply
  // gets a big card right in the conversation, not just a line in the feed.
  // Also remembered for the right panel's compact "current trip" summary
  // (see renderTripPanel) — the map's own geometry comes from the server
  // instead (/api/map-state), but this richer result is what the panel's
  // text needs (eta_human, etc. that map-state doesn't carry).
  if (who === "agent") {
    const arrivalEvent = pending.find(
      (event) => event.tool === "next_arrivals" && event.result && Array.isArray(event.result.arrivals) && event.result.arrivals.length
    );
    if (arrivalEvent) {
      row.append(renderArrivalCard(arrivalEvent.result));
      lastLiveEvent = { kind: "arrival", result: arrivalEvent.result };
      renderTripPanel();
    }

    const tripEvent = pending.find(
      (event) => event.tool === "plan_trip" && event.result && Array.isArray(event.result.options) && event.result.options.length
    );
    if (tripEvent) {
      row.append(renderTripCard(tripEvent.result));
      lastLiveEvent = { kind: "trip", result: tripEvent.result };
      renderTripPanel();
    }
  }

  // Tool-call details (which tools ran, args, raw reasons) live only in the
  // Developer view now — the conversation itself shows just the reply plus
  // the arrival/trip card above, never a "tool ×N" chip trail.
  if (who === "agent") {
    pending = [];
  }

  els.transcript.append(row);
  els.transcript.scrollTop = els.transcript.scrollHeight;
}

// ------------------------------------------------- server-side tool calls

function formatArgs(args) {
  const keys = Object.keys(args || {});
  if (!keys.length) return "";
  return keys.map((k) => `${k}: ${JSON.stringify(args[k])}`).join("\n");
}

// Reuses the header's own bus glyph so every "this is a bus" signal in the
// page is the same icon — just recoloured per route category.
const BUS_ICON_SVG =
  '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">' +
  '<path d="M8 6v6"/><path d="M15 6v6"/><path d="M2 12h19.6"/>' +
  '<path d="M18 18h3s.5-1.7.8-2.8c.1-.4.2-.8.2-1.2 0-.4-.1-.8-.2-1.2l-1.4-5C20.1 6.8 19.1 6 18 6H4a2 2 0 0 0-2 2v10h3"/>' +
  '<circle cx="7" cy="18" r="2"/><path d="M9 18h5"/><circle cx="16" cy="18" r="2"/></svg>';

const TRAIN_ICON_SVG =
  '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">' +
  '<rect x="5" y="3" width="14" height="13" rx="4"/><path d="M5 11h14"/>' +
  '<path d="M9 19l-2 3"/><path d="M15 19l2 3"/><circle cx="8.5" cy="14" r="0.6" fill="currentColor" stroke="none"/>' +
  '<circle cx="15.5" cy="14" r="0.6" fill="currentColor" stroke="none"/></svg>';

// Reused for both "transfer between legs" (conversation trip card) and the
// tool-feed's not_found/ambiguous chip elsewhere — a generic "switch" glyph.
const TRANSFER_ICON_SVG =
  '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">' +
  '<path d="M3 12a9 9 0 0 1 15-6.7L21 8"/><path d="M21 3v5h-5"/>' +
  '<path d="M21 12a9 9 0 0 1-15 6.7L3 16"/><path d="M3 21v-5h5"/></svg>';

const WALK_ICON_SVG =
  '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">' +
  '<circle cx="13" cy="4" r="1.5" fill="currentColor" stroke="none"/><path d="M10.5 21l1.5-6-2-2 .5-4.5 3 2.5 2.5 1"/>' +
  '<path d="M12.5 9.5L11 15l-3 3"/><path d="M14 12l3 1 1.5 4"/></svg>';

const TRIP_ICONS = { bus: BUS_ICON_SVG, train: TRAIN_ICON_SVG, transfer: TRANSFER_ICON_SVG, walk: WALK_ICON_SVG };

// ETA rows styled like where-bus's own stop-selected view: icon chip, route
// badge coloured by category (RapidKL maroon / MRT Feeder slate), bold ETA.
function renderEtaList(arrivals) {
  const list = el("div", "eta-list");
  for (const arrival of arrivals) {
    const category = arrival.category === "rapid-bus-mrtfeeder" ? "rapid-bus-mrtfeeder" : "rapid-bus-kl";
    const row = el("div", "eta-row");

    const chip = el("span", "icon-chip");
    chip.style.color = category === "rapid-bus-mrtfeeder" ? "var(--color-route-feeder)" : "var(--color-route-rapidkl)";
    chip.innerHTML = BUS_ICON_SVG;

    const badge = el("span", `route-badge ${category}`, `Route ${arrival.route}`);
    const time = el("span", "eta-time", arrival.eta_human);

    row.append(chip, badge, time);
    list.append(row);
  }
  return list;
}

// "3 minutes" / "22 minutes" -> {num: "3", unit: "min"}; "arriving now" has
// no number to extract, so it gets its own big word instead.
function bigEta(arrival) {
  if (arrival.eta_human === "arriving now") return { num: "NOW", unit: "" };
  const match = /\d+/.exec(arrival.eta_human);
  return { num: match ? match[0] : "?", unit: "min" };
}

// The arrival card attached under the agent's reply in the conversation
// itself — route badge, stop name, one big number, up to 2 following buses.
function renderArrivalCard(result) {
  const [heroArrival, ...rest] = result.arrivals;
  const following = rest.slice(0, 2);
  const category = heroArrival.category === "rapid-bus-mrtfeeder" ? "rapid-bus-mrtfeeder" : "rapid-bus-kl";

  const card = el("div", "arrival-card");

  const top = el("div", "arrival-top");
  top.append(el("span", `route-badge ${category}`, `Route ${heroArrival.route}`));
  if (result.stop) top.append(el("span", "arrival-stop", result.stop));
  card.append(top);

  const { num, unit } = bigEta(heroArrival);
  const hero = el("div", "arrival-hero");
  hero.append(el("span", "arrival-num", num));
  if (unit) hero.append(el("span", "arrival-unit", unit));
  card.append(hero);

  if (following.length) {
    const list = el("div", "arrival-following");
    for (const arrival of following) {
      list.append(el("span", null, `Then Route ${arrival.route} · ${arrival.eta_human}`));
    }
    card.append(list);
  }

  return card;
}

// One row per leg (bus or rail), with a "Transfer" divider between legs —
// plan_trip's rail-inclusive options are a {steps: [...]} sequence, not a
// single arrival, so this is its own card shape rather than reusing
// renderArrivalCard.
function renderTripStep(step) {
  const row = el("div", "trip-step");
  if (step.mode === "bus") {
    const category = step.category === "rapid-bus-mrtfeeder" ? "rapid-bus-mrtfeeder" : "rapid-bus-kl";
    row.append(el("span", `route-badge ${category}`, `Route ${step.route}`));
    row.append(el("span", "trip-step-detail", `${step.board_at} → ${step.alight_at} · ${step.eta_human}`));
  } else if (step.mode === "walk") {
    const chip = el("span", "icon-chip");
    chip.innerHTML = WALK_ICON_SVG;
    row.append(chip);
    row.append(el("span", "trip-step-detail", `Walk to ${step.alight_at} · about ${step.minutes} min`));
  } else {
    row.append(el("span", "route-badge rapid-bus-mrtfeeder", step.line));
    const plural = step.stops !== 1 ? "s" : "";
    row.append(el("span", "trip-step-detail", `toward ${step.direction} · ${step.stops} stop${plural} · ${step.to_station}`));
  }
  return row;
}

function renderTripSteps(steps) {
  const wrap = el("div", "trip-steps");
  steps.forEach((step, i) => {
    if (i > 0) wrap.append(el("div", "trip-transfer", "Transfer"));
    wrap.append(renderTripStep(step));
  });
  return wrap;
}

// Expands each leg into its numbered lines: a leg's own step, plus — unless
// it's the last leg — a separate "Get off at X" step before the next leg,
// so a 2-leg trip reads as 3 numbered instructions, matching how a rider
// actually experiences it (ride, alight, ride again).
function tripNumberedRows(steps) {
  const rows = [];
  steps.forEach((step, i) => {
    const isLast = i === steps.length - 1;
    if (step.mode === "bus") {
      const dest = isLast ? ` to ${step.alight_at}` : "";
      rows.push({ icon: "bus", text: `Bus ${step.route} from ${step.board_at}${dest} — next in ${step.eta_human}` });
      if (!isLast) rows.push({ icon: "transfer", text: `Get off at ${step.alight_at}` });
    } else if (step.mode === "walk") {
      rows.push({ icon: "walk", text: `Walk to ${step.alight_at} — about ${step.minutes} min` });
    } else {
      const plural = step.stops !== 1 ? "s" : "";
      rows.push({ icon: "train", text: `${step.line} toward ${step.direction} — ${step.stops} stop${plural} to ${step.to_station}` });
      if (!isLast) rows.push({ icon: "transfer", text: `Get off at ${step.to_station}` });
    }
  });
  return rows;
}

function tripTotalStops(steps) {
  return steps.reduce((sum, s) => sum + (s.mode === "rail" ? s.stops : 0), 0);
}

function renderOneTripCard(option, index, label, total) {
  const card = el("div", "trip-card");
  if (label) card.append(el("p", "trip-card-label", label));

  const list = el("ol", "trip-num-steps");
  tripNumberedRows(option.steps).forEach((row, i) => {
    const li = el("li", "trip-num-step");
    li.append(el("span", "trip-num", String(i + 1)));
    const icon = el("span", "trip-icon");
    icon.innerHTML = TRIP_ICONS[row.icon];
    li.append(icon, el("span", "trip-num-text", row.text));
    list.append(li);
  });
  card.append(list);

  const foot = el("div", "trip-card-foot");
  const stops = tripTotalStops(option.steps);
  foot.append(el("span", null, stops ? `${stops} stop${stops !== 1 ? "s" : ""}` : "Direct"));
  card.append(foot);

  // Tapping either card shows that option on the map — only meaningful
  // (and only made to look clickable) when there's more than one to switch
  // between.
  if (total > 1) {
    card.classList.add("trip-card-selectable");
    card.addEventListener("click", () => {
      setActiveOption(index);
      card.parentElement.querySelectorAll(".trip-card").forEach((c) => c.classList.toggle("trip-card-active", c === card));
    });
    if (index === 0) card.classList.add("trip-card-active");
  }

  return card;
}

// One card per option — alternatives are separate, clearly labelled "Option
// N" cards, not folded into the first card with an inline "Or:".
function renderTripCard(result) {
  activeOptionIndex = 0; // a fresh trip always starts on its first option
  const wrap = el("div", "trip-cards");
  wrap.append(el("p", "arrival-stop", `${result.from_stop} → ${result.to_stop}`));
  result.options.forEach((option, i) =>
    wrap.append(renderOneTripCard(option, i, i === 0 ? null : `Option ${i + 1}`, result.options.length))
  );
  return wrap;
}

// A rider doesn't care that this came from "next_arrivals" — they care what
// stop or trip it's about. Falls back to a plain tool label when a result
// has nothing more specific to show (get_now, a failed lookup with no stop).
const TOOL_LABELS = {
  next_arrivals: "Bus arrivals",
  plan_trip: "Trip",
  find_stop: "Stop lookup",
  find_nearby_stops: "Nearby stops",
  set_arrival_alert: "Arrival alert",
  get_now: "Current time",
};

function friendlyTitle(event) {
  const r = event.result || {};
  if (event.tool === "next_arrivals" && r.stop) return r.stop;
  if (event.tool === "plan_trip" && r.from_stop) return `${r.from_stop} → ${r.to_stop}`;
  if (event.tool === "find_stop" && r.stop) return r.stop;
  if (event.tool === "set_arrival_alert" && r.ok) return "Alert set";
  return TOOL_LABELS[event.tool] || event.tool;
}

function renderCall(event) {
  clearEmpty(els.calls);

  const failed = event.result && event.result.ok === false;
  event.failed = failed;

  const card = document.createElement("article");
  card.className = `call${failed ? " warn" : ""}`;
  card.dataset.seq = event.seq;

  const head = document.createElement("header");
  head.append(el("span", "call-title", friendlyTitle(event)));
  card.append(head);

  // Rider-facing first: the tool's own spoken message, plus a structured
  // eta list when there's one to show.
  const msg = document.createElement("p");
  msg.className = "call-msg";
  msg.textContent = (event.result && event.result.message) || "(no response)";
  card.append(msg);

  if (event.result && Array.isArray(event.result.arrivals) && event.result.arrivals.length) {
    card.append(renderEtaList(event.result.arrivals));
  } else if (event.result && Array.isArray(event.result.options) && event.result.options.length) {
    card.append(renderTripSteps(event.result.options[0].steps));
  }

  // Judge-facing plumbing (tool name, raw query, timestamp, raw reason
  // code) collapsed behind one toggle — visible on request, not by default.
  const details = document.createElement("details");
  details.className = "call-details";
  details.append(el("summary", null, "Details"));

  const meta = el("div", "call-meta");
  meta.append(el("span", "call-name", event.tool), el("span", "call-time", event.at));
  details.append(meta);

  const args = formatArgs(event.arguments);
  if (args) {
    const pre = document.createElement("pre");
    pre.className = "call-args";
    pre.textContent = args;
    details.append(pre);
  }

  if (failed && event.result.reason) {
    details.append(el("span", "call-reason", event.result.reason.replace(/_/g, " ")));
  }

  card.append(details);

  els.calls.append(card);
  els.calls.scrollTop = els.calls.scrollHeight;
}

// "Something to show" for the map: a resolved stop/station, a nearby-stops
// list, live arrivals, or a planned trip — not get_now, and not a failed or
// ambiguous lookup with no point to place. Matches what mapstate.py's
// set_from_* functions each actually require to produce a pin.
function eventHasMapContent(event) {
  const r = event.result;
  if (!r || !r.ok) return false;
  switch (event.tool) {
    case "find_stop":
      return Boolean(r.stop);
    case "find_nearby_stops":
      return Array.isArray(r.nearby) && r.nearby.length > 0;
    case "next_arrivals":
      return Array.isArray(r.arrivals) && r.arrivals.length > 0;
    case "plan_trip":
      return Array.isArray(r.options) && r.options.length > 0;
    default:
      return false;
  }
}

async function pollEvents() {
  try {
    const res = await fetch(`/api/events?since=${eventCursor}&call_id=${encodeURIComponent(currentCallId || "")}`);
    const data = await res.json();
    eventCursor = data.cursor;
    for (const event of data.events) {
      renderCall(event);
      pending.push(event);
      if (event.alert) showAlert(event.result.message);
      if (event.alert || eventHasMapContent(event)) revealPanel();
    }
  } catch (_) {
    /* transient; the next tick retries */
  }
}

// ---------------------------------------------------------- arrival alerts

let alertTimer = null;

function beep() {
  const ctx = new (window.AudioContext || window.webkitAudioContext)();
  const osc = ctx.createOscillator();
  const gain = ctx.createGain();
  osc.type = "sine";
  osc.frequency.value = 880;
  gain.gain.setValueAtTime(0.001, ctx.currentTime);
  gain.gain.exponentialRampToValueAtTime(0.2, ctx.currentTime + 0.02);
  gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + 0.35);
  osc.connect(gain).connect(ctx.destination);
  osc.start();
  osc.stop(ctx.currentTime + 0.4);
}

function showAlert(message) {
  els.alertText.textContent = message;
  els.alertBanner.hidden = false;
  beep();
  clearTimeout(alertTimer);
  alertTimer = setTimeout(() => (els.alertBanner.hidden = true), 8000);
}

// ---------------------------------------------------------------- audio

function playChunk(int16) {
  const buffer = audioCtx.createBuffer(1, int16.length, SAMPLE_RATE);
  const channel = buffer.getChannelData(0);
  for (let i = 0; i < int16.length; i++) channel[i] = int16[i] / 32768;

  const source = audioCtx.createBufferSource();
  source.buffer = buffer;
  source.connect(audioCtx.destination);

  const now = audioCtx.currentTime;
  if (playHead < now) playHead = now + 0.04;
  source.start(playHead);
  playHead += buffer.duration;

  scheduled.push(source);
  source.onended = () => {
    const i = scheduled.indexOf(source);
    if (i >= 0) scheduled.splice(i, 1);
  };
}

function stopPlayback() {
  for (const source of scheduled) {
    try {
      source.stop();
    } catch (_) {
      /* already finished */
    }
  }
  scheduled = [];
  playHead = 0;
}

// ------------------------------------------------------------- location

// Silent by default (called on every call start, whether or not the caller
// ever asks for a nearby stop) — find_nearby_stops just won't work without
// it, which the agent already handles as a normal ok:false reason. The
// explicit "Share location" button passes onDone to show a confirmation.
function shareLocation(onDone) {
  if (!navigator.geolocation) return onDone && onDone(false);
  navigator.geolocation.getCurrentPosition(
    (pos) => {
      fetch("/api/location", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ lat: pos.coords.latitude, lon: pos.coords.longitude, call_id: currentCallId }),
      })
        .then(() => onDone && onDone(true))
        .catch(() => onDone && onDone(false));
    },
    () => onDone && onDone(false),
    { timeout: 8000 }
  );
}

const shareLocationBtn = document.getElementById("share-location");
shareLocationBtn.addEventListener("click", () => {
  shareLocationBtn.disabled = true;
  shareLocationBtn.textContent = "Locating…";
  shareLocation((ok) => {
    shareLocationBtn.disabled = false;
    shareLocationBtn.classList.toggle("confirmed", ok);
    shareLocationBtn.textContent = ok ? "Location shared" : "Couldn't get location";
    setTimeout(() => {
      shareLocationBtn.classList.remove("confirmed");
      shareLocationBtn.textContent = "Share location";
    }, 2500);
  });
});

// --------------------------------------------------------------- session

async function start() {
  // Only one Start/End call button on screen, ever: the hero's big centred
  // one is for the empty state only, the bar's is for every state after —
  // flip the instant a call is requested (not on the first transcript
  // line), so there's no window where both are visible together.
  document.body.classList.add("call-started");
  clearEmpty(els.transcript);

  // A fresh call_id before touching anything scoped to it (map-state,
  // events, location) — see currentCallId's own comment. Reset whatever
  // the previous call left behind so the panel opens onto this call's own
  // (empty, until a tool call fills it) state, never a leftover one.
  try {
    const data = await fetch("/api/call/start", { method: "POST" }).then((r) => r.json());
    currentCallId = data.call_id;
  } catch (_) {
    currentCallId = null;
  }
  eventCursor = 0;
  pending = [];
  lastLiveEvent = null;
  lastMapState = null;
  if (map) for (const layer of mapLayers) map.removeLayer(layer);
  mapLayers = [];

  // Single-column until THIS call's own first result reveals the panel
  // (see revealPanel, called from pollEvents) — a previous call's open
  // panel must not carry over.
  panelRevealed = false;
  document.body.classList.remove("panel-revealed", "tools-open");
  document.getElementById("tools-pane").classList.remove("tools-visible");
  clearInterval(mapPollTimer);
  mapPollTimer = null;
  renderTripPanel();

  shareLocation(); // ask now, while the call-start click still counts as a user gesture
  resetTimer(); // clear the previous call's duration
  setStatus("Connecting", "busy");
  els.talk.disabled = true;

  let token;
  try {
    const res = await fetch("/api/token");
    if (!res.ok) throw new Error((await res.text()).slice(0, 200));
    token = (await res.json()).token;
  } catch (err) {
    setStatus("No token", "error");
    addLine("agent", `Could not mint a token: ${err.message}`);
    els.talk.disabled = false;
    return;
  }

  const config = await fetch("/api/config").then((r) => r.json());
  if (!config.agent_id) {
    setStatus("No agent", "error");
    addLine("agent", "Run scripts/create_agent.py first, then reload this page.");
    els.talk.disabled = false;
    return;
  }
  currentAgentId = config.agent_id;

  try {
    micStream = await navigator.mediaDevices.getUserMedia({
      audio: {
        echoCancellation: true,
        noiseSuppression: false,
        autoGainControl: true,
        sampleRate: SAMPLE_RATE,
      },
    });
  } catch (err) {
    setStatus("No microphone", "error");
    addLine("agent", "Microphone access was blocked. Allow it and try again.");
    els.talk.disabled = false;
    return;
  }

  audioCtx = new AudioContext({ sampleRate: SAMPLE_RATE });
  await audioCtx.audioWorklet.addModule("/worklet.js");

  ws = new WebSocket(`${WS_URL}?token=${encodeURIComponent(token)}`);

  ws.onopen = () => {
    ws.send(JSON.stringify({ type: "session.update", session: { agent_id: config.agent_id } }));
  };

  ws.onmessage = (event) => {
    const msg = JSON.parse(event.data);

    switch (msg.type) {
      case "session.ready":
        live = true;
        setStatus("Live", "live");
        els.listening.dataset.live = "true";
        els.talk.disabled = false;
        els.talk.textContent = "End call";
        els.talk.classList.add("ending");
        startTimer();
        pollTimer = setInterval(pollEvents, POLL_MS);
        if (pendingPromptHint) {
          clearEmpty(els.transcript);
          els.transcript.append(el("p", "prompt-hint", `Try saying: "${pendingPromptHint}"`));
          pendingPromptHint = null;
        }
        break;

      case "input.speech.started":
        stopPlayback(); // the caller cut in
        break;

      case "transcript.user":
        addLine("user", msg.text);
        break;

      case "transcript.agent":
        addLine("agent", msg.text);
        break;

      case "reply.audio":
        playChunk(fromBase64(msg.data));
        break;

      case "reply.done":
        if (msg.status === "interrupted") stopPlayback();
        break;

      case "session.error":
        setStatus(msg.code || "Error", "error");
        addLine("agent", msg.message || "Session error");
        break;

      case "session.ended":
        cleanup();
        break;
    }
  };

  ws.onerror = () => setStatus("Connection error", "error");
  ws.onclose = () => cleanup();

  workletNode = new AudioWorkletNode(audioCtx, "pcm-processor");
  workletNode.port.onmessage = ({ data }) => {
    if (ws && ws.readyState === WebSocket.OPEN && live) {
      ws.send(JSON.stringify({ type: "input.audio", audio: toBase64(data) }));
    }
  };
  audioCtx.createMediaStreamSource(micStream).connect(workletNode);
  workletNode.connect(audioCtx.destination); // keeps the graph pulling
}

function stop() {
  // Closing the socket bare leaves a 30s resume window that still bills.
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ type: "session.end" }));
    setStatus("Ending", "busy");
    els.talk.disabled = true;
    setTimeout(() => ws && ws.close(), 1200);
  } else {
    cleanup();
  }
}

function cleanup() {
  if (!live && !micStream && !audioCtx) return;
  live = false;
  els.listening.dataset.live = "false";
  stopTimer(); // freeze the duration, don't clear it

  clearInterval(pollTimer);
  pollTimer = null;
  clearInterval(mapPollTimer);
  mapPollTimer = null;
  stopPlayback();

  if (micStream) micStream.getTracks().forEach((t) => t.stop());
  micStream = null;

  if (workletNode) workletNode.disconnect();
  workletNode = null;

  if (audioCtx) audioCtx.close();
  audioCtx = null;

  ws = null;
  els.talk.disabled = false;
  els.talk.textContent = "Start call";
  els.talk.classList.remove("ending");
  setStatus("Idle", "idle");
  pollEvents(); // catch anything that landed as the call closed
  pollMapState();
}

els.talk.addEventListener("click", () => (live ? stop() : start()));

fetch("/api/config")
  .then((r) => r.json())
  .then((c) => {
    currentAgentId = c.agent_id || "";
  })
  .catch(() => {});

// ------------------------------------------------------------------ hero

document.getElementById("hero-talk").addEventListener("click", () => els.talk.click());

for (const button of document.querySelectorAll(".example")) {
  button.addEventListener("click", () => {
    pendingPromptHint = button.dataset.prompt;
    if (!live) els.talk.click();
  });
}

// ------------------------------------------------------------ tools panel

const toolsToggle = document.getElementById("tools-toggle");
const toolsBackdrop = document.getElementById("tools-backdrop");
const toolsPane = document.getElementById("tools-pane");

// Set once revealPanel() first shows something this call, cleared again by
// start() for the next one. Gates #tools-toggle's own visibility (see CSS)
// separately from tools-open, which is just "currently expanded vs
// collapsed" and can be flipped back and forth by the user after reveal.
let panelRevealed = false;

function setToolsOpen(open) {
  document.body.classList.toggle("tools-open", open);
  toolsToggle.setAttribute("aria-expanded", String(open));
}

// The one place the panel goes from genuinely absent (display:none) to
// visible — see the .tools-visible/.tools-open CSS. Two steps, one frame
// apart, so the width transition actually plays: adding display:flex and
// starting the transition in the same tick never animates.
function revealPanel() {
  if (panelRevealed) return;
  panelRevealed = true;
  document.body.classList.add("panel-revealed");
  toolsPane.classList.add("tools-visible");
  void toolsPane.offsetWidth; // force layout before tools-open's width/height jumps
  setToolsOpen(true);
  requestAnimationFrame(() => {
    if (map) map.invalidateSize(); // sized against a display:none container until now
    if (!mapPollTimer) mapPollTimer = setInterval(pollMapState, MAP_POLL_MS);
    pollMapState();
  });
  // The pane's width (desktop) or height (mobile) is still animating at
  // that first invalidateSize() — Leaflet only loads tiles for whatever
  // size it saw then, leaving the rest of the newly-grown box blank grey
  // once the transition finishes. One more call after it settles fixes
  // that. A plain timeout (not transitionend) so this still fires under
  // prefers-reduced-motion, where the transition is instant/skipped.
  setTimeout(() => {
    if (map) map.invalidateSize();
  }, 380);
}

toolsToggle.addEventListener("click", () => setToolsOpen(!document.body.classList.contains("tools-open")));
toolsBackdrop.addEventListener("click", () => setToolsOpen(false)); // tap-outside-to-close, mobile sheet only
// Stays closed (and .pane.tools stays display:none) until revealPanel()
// opens it on this call's first result — see pollEvents below.

// --------------------------------------------------------------- live map
//
// Leaflet, read-only — panning/zooming only, no editing. Tiles are the
// standard OpenStreetMap raster server — CARTO's own tiles started
// returning "API key required" on the deployed site once its free tier
// changed, so this switched back to the tile source that never needed one.
// Greyscale is a CSS filter on .leaflet-tile-pane only (see index.html), so
// markers, route polylines and a rail line's own colour stay on top,
// unfiltered. Geometry comes from /api/map-state (app/mapstate.py):
// stop-to-stop polylines built from coordinates already loaded for the ETA
// engine, never the full GTFS shape files — no memory cost beyond what
// next_arrivals already needed. Polled on MAP_POLL_MS; map-state itself is
// cheap (no network call to data.gov.my, just a read of whatever's cached).

let map = null;
let mapLayers = [];
let lastMapState = null;
let activeOptionIndex = 0;

function initMap() {
  if (typeof L === "undefined" || map) return; // CDN blocked/slow — page still works without it
  map = L.map("map", { attributionControl: true }).setView([3.139, 101.6869], 12); // Kuala Lumpur
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
    maxZoom: 19,
  }).addTo(map);
}

// Small always-visible dot; used for live vehicles, where a text label per
// bus would clutter the map more than it helps.
function mapDot(cls) {
  return L.divIcon({ className: "", html: `<span class="map-dot ${cls}"></span>`, iconSize: [14, 14] });
}

// A labelled pin — Start/Transfer/Destination need to read at a glance, not
// require a tap/hover to identify, so the label is baked into the icon
// itself rather than a tooltip.
function mapPin(label, cls) {
  return L.divIcon({
    className: "",
    html: `<div class="map-pin ${cls}"><span class="map-pin-dot"></span><span class="map-pin-label">${label}</span></div>`,
    iconSize: null,
    iconAnchor: [7, 7],
  });
}

const ROLE_LABEL = { board: "Start", transfer: "Transfer", alight: "Destination" };
const ROLE_CLASS = { board: "start", transfer: "transfer", alight: "destination", stop: "stop" };

function haversineKm(lat1, lon1, lat2, lon2) {
  const R = 6371;
  const dLat = ((lat2 - lat1) * Math.PI) / 180;
  const dLon = ((lon2 - lon1) * Math.PI) / 180;
  const a = Math.sin(dLat / 2) ** 2 + Math.cos((lat1 * Math.PI) / 180) * Math.cos((lat2 * Math.PI) / 180) * Math.sin(dLon / 2) ** 2;
  return R * 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
}

function setActiveOption(index) {
  activeOptionIndex = index;
  if (lastMapState) renderMap(lastMapState);
}

function renderMap(state) {
  lastMapState = state;
  if (!map) return;
  for (const layer of mapLayers) map.removeLayer(layer);
  mapLayers = [];

  const options = state.options || [];
  const option = options[Math.min(activeOptionIndex, options.length - 1)] || options[0];

  const bounds = [];
  const addBounds = (lat, lon) => bounds.push([lat, lon]);

  for (const stop of (option && option.stops) || []) {
    const label = ROLE_LABEL[stop.role] || stop.name;
    mapLayers.push(L.marker([stop.lat, stop.lon], { icon: mapPin(label, ROLE_CLASS[stop.role] || "stop") }).addTo(map));
    addBounds(stop.lat, stop.lon);
  }
  for (const leg of (option && option.legs) || []) {
    if (!leg.points || !leg.points.length) continue;
    const color = leg.mode === "rail" ? leg.color || "#6b7280" : leg.mode === "walk" ? "#9ca3af" : "#111827";
    const style = { color, weight: 5, opacity: 0.85 };
    if (leg.mode === "walk") {
      style.weight = 3;
      style.dashArray = "2 8"; // a walking leg reads as a dotted line, distinct from a ridden route
    }
    mapLayers.push(L.polyline(leg.points, style).addTo(map));
    leg.points.forEach(([lat, lon]) => addBounds(lat, lon));
  }
  for (const v of (option && option.vehicles) || []) {
    const cls = v.category === "rapid-bus-mrtfeeder" ? "vehicle-feeder" : "vehicle";
    mapLayers.push(L.marker([v.lat, v.lon], { icon: mapDot(cls) }).bindTooltip(`Route ${v.route}`).addTo(map));
  }

  // The user's own location only helps fit the view when it's actually
  // near the trip — testing from across the country (or just a stale/
  // inaccurate reading) shouldn't zoom the map out to a useless country-
  // wide view.
  if (state.location && bounds.length) {
    const nearAny = bounds.some(([lat, lon]) => haversineKm(state.location.lat, state.location.lon, lat, lon) <= 50);
    if (nearAny) {
      mapLayers.push(L.marker([state.location.lat, state.location.lon], { icon: mapDot("you") }).addTo(map));
      addBounds(state.location.lat, state.location.lon);
    }
  } else if (state.location && !bounds.length) {
    mapLayers.push(L.marker([state.location.lat, state.location.lon], { icon: mapDot("you") }).addTo(map));
    addBounds(state.location.lat, state.location.lon);
  }

  if (bounds.length) map.fitBounds(bounds, { padding: [36, 36], maxZoom: 16, animate: false });
}

async function pollMapState() {
  try {
    const state = await fetch(`/api/map-state?call_id=${encodeURIComponent(currentCallId || "")}`).then((r) => r.json());
    renderMap(state);
  } catch (_) {
    /* transient; the next tick retries */
  }
}

// Polling (and the panel itself) only starts once a call begins — see
// start(). Before that there's nothing to show and nothing scoped to poll
// for yet (see currentCallId).
let mapPollTimer = null;

initMap();

// The right panel's compact "current trip" summary — reuses the same
// step-row markup as the old inline card, driven by the richer result
// addLine() already tracked in lastLiveEvent (map-state's own payload is
// geometry-only, no eta_human/etc.).
function renderTripPanel() {
  const panel = document.getElementById("trip-panel");
  panel.textContent = "";
  if (!lastLiveEvent) {
    panel.append(el("p", "empty-note", "Ask about a stop or trip to see it here."));
    return;
  }
  if (lastLiveEvent.kind === "trip") {
    panel.append(renderTripSteps(lastLiveEvent.result.options[0].steps));
  } else {
    panel.append(renderEtaList(lastLiveEvent.result.arrivals));
  }
}

// ----------------------------------------------------------- demo data

const infoEls = {
  btn: document.getElementById("info-btn"),
  modal: document.getElementById("info"),
  body: document.getElementById("info-body"),
};

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text != null) node.textContent = text;
  return node;
}

function block(title, source, note) {
  const wrap = el("section");
  const head = el("div", "block-head");
  head.append(el("h3", null, title), el("span", "src", source));
  wrap.append(head);
  if (note) wrap.append(el("p", "block-note", note));
  return wrap;
}

function pills(items, cls) {
  const row = el("div", "pillrow");
  for (const item of items) row.append(el("span", cls ? `pill ${cls}` : "pill", item));
  return row;
}

function kv(pairs) {
  const dl = el("dl", "kv");
  for (const [key, value] of pairs) {
    dl.append(el("dt", null, key), el("dd", null, value));
  }
  return dl;
}

function renderInfo(data) {
  infoEls.body.textContent = "";

  const who = block("Who it is", "agent.json");
  who.append(kv([
    ["Name", data.agent.name],
    ["Voice", data.agent.voice],
  ]));
  who.append(el("p", "block-note", "Tools it can call:"));
  who.append(pills(data.agent.tools.map((t) => t.name), "tool"));
  if (data.agent.keyterms.length) {
    const details = document.createElement("details");
    details.append(
      el("summary", null, `Show ${data.agent.keyterms.length} boosted words`),
      pills(data.agent.keyterms)
    );
    who.append(details);
  }

  const sees = block(
    "What it can see",
    "your bus arrival API",
    "Live RapidKL + MRT Feeder data, refreshed about every 45 seconds — none of it is in agent.json."
  );
  sees.append(kv([
    ["Stops", data.network.stops],
    ["Routes", data.network.routes],
    ["Buses now", data.network.active_vehicles],
    ["Service hours", data.network.service_hours],
  ]));

  const footer = el(
    "p",
    "modal-footer-note",
    `Agent ID: ${currentAgentId || "not published"} · $4.50/hr, billed per second`
  );

  infoEls.body.append(who, sees, footer);
}

function onInfoKey(event) {
  if (event.key === "Escape") closeInfo();
}

async function openInfo() {
  infoEls.modal.hidden = false;
  document.addEventListener("keydown", onInfoKey);
  infoEls.modal.querySelector(".icon-btn").focus();

  // Always refetch — slots change as the agent books them.
  try {
    renderInfo(await fetch("/api/demo-info").then((r) => r.json()));
  } catch (_) {
    infoEls.body.textContent = "Could not load agent info. Is the bus arrival API running?";
  }
}

function closeInfo() {
  infoEls.modal.hidden = true;
  document.removeEventListener("keydown", onInfoKey);
  infoEls.btn.focus();
}

infoEls.btn.addEventListener("click", openInfo);
for (const node of infoEls.modal.querySelectorAll("[data-close]")) {
  node.addEventListener("click", closeInfo);
}
