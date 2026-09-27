const SAMPLE_RATE = 24000;
const WS_URL = "wss://agents.assemblyai.com/v1/ws";
const POLL_MS = 400;

const els = {
  talk: document.getElementById("talk"),
  status: document.getElementById("status"),
  transcript: document.getElementById("transcript"),
  calls: document.getElementById("calls"),
  count: document.getElementById("call-count"),
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

// Playback scheduling. Holding the sources lets barge-in cut the agent off.
let playHead = 0;
let scheduled = [];

// Tool calls arrive out-of-band from the bus arrival API's own log. Buffer them so
// the next agent reply can show which ones fed it.
let eventCursor = 0;
let pollTimer = null;
let pending = [];
let callTotal = 0;

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
  if (who === "agent") {
    const arrivalEvent = pending.find(
      (event) => event.tool === "next_arrivals" && event.result && Array.isArray(event.result.arrivals) && event.result.arrivals.length
    );
    if (arrivalEvent) row.append(renderArrivalCard(arrivalEvent.result));

    const tripEvent = pending.find(
      (event) => event.tool === "plan_trip" && event.result && Array.isArray(event.result.options) && event.result.options.length
    );
    if (tripEvent) row.append(renderTripCard(tripEvent.result));
  }

  // Tie this reply to the tool calls that produced it. Repeats of the same
  // tool in one batch (e.g. a chatty get_now) collapse into one "tool ×N"
  // chip rather than a wall of identical buttons.
  if (who === "agent" && pending.length) {
    const used = document.createElement("div");
    used.className = "used";
    const grouped = new Map(); // tool name -> { seqs, failed (of the latest call) }
    for (const event of pending) {
      const group = grouped.get(event.tool) || { seqs: [] };
      group.seqs.push(event.seq);
      group.failed = event.failed;
      grouped.set(event.tool, group);
    }
    for (const [tool, group] of grouped) {
      const count = group.seqs.length;
      const lastSeq = group.seqs[count - 1];
      const chip = document.createElement("button");
      chip.type = "button";
      chip.className = "chip" + (group.failed ? " warn" : "");
      chip.textContent = count > 1 ? `${tool} ×${count}` : tool;
      chip.title = count > 1 ? `Show the latest of ${count} calls` : "Show this call";
      chip.addEventListener("click", () => revealCall(lastSeq));
      used.append(chip);
    }
    row.append(used);
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

// Same card shape as an arrival, one route badge/hero number/following list —
// plan_trip's options are already {route, category, eta_human, ...}, so it
// reuses bigEta and every arrival-card CSS class as-is.
function renderTripCard(result) {
  const [primary, ...rest] = result.options;
  const category = primary.category === "rapid-bus-mrtfeeder" ? "rapid-bus-mrtfeeder" : "rapid-bus-kl";

  const card = el("div", "arrival-card");

  const top = el("div", "arrival-top");
  top.append(el("span", `route-badge ${category}`, `Route ${primary.route}`));
  top.append(el("span", "arrival-stop", `${result.from_stop} → ${result.to_stop}`));
  card.append(top);

  const { num, unit } = bigEta(primary);
  const hero = el("div", "arrival-hero");
  hero.append(el("span", "arrival-num", num));
  if (unit) hero.append(el("span", "arrival-unit", unit));
  card.append(hero);

  if (rest.length) {
    const list = el("div", "arrival-following");
    for (const opt of rest) list.append(el("span", null, `Or Route ${opt.route} · ${opt.eta_human}`));
    card.append(list);
  }

  return card;
}

function renderCall(event) {
  clearEmpty(els.calls);

  const failed = event.result && event.result.ok === false;
  event.failed = failed;

  const card = document.createElement("article");
  card.className = `call${failed ? " warn" : ""}`;
  card.dataset.seq = event.seq;

  const head = document.createElement("header");
  const name = document.createElement("span");
  name.className = "call-name";
  name.textContent = event.tool;
  const time = document.createElement("span");
  time.className = "call-time";
  time.textContent = event.at;
  head.append(name, time);
  card.append(head);

  const args = formatArgs(event.arguments);
  if (args) {
    const pre = document.createElement("pre");
    pre.className = "call-args";
    pre.textContent = args;
    card.append(pre);
  }

  if (failed && event.result.reason) {
    const reason = document.createElement("span");
    reason.className = "call-reason";
    reason.textContent = event.result.reason.replace(/_/g, " ");
    card.append(reason);
  }

  const msg = document.createElement("p");
  msg.className = "call-msg";
  msg.textContent = (event.result && event.result.message) || "(no message)";
  card.append(msg);

  if (event.result && Array.isArray(event.result.arrivals) && event.result.arrivals.length) {
    card.append(renderEtaList(event.result.arrivals));
  } else if (event.result && Array.isArray(event.result.options) && event.result.options.length) {
    card.append(renderEtaList(event.result.options));
  }

  els.calls.append(card);
  els.calls.scrollTop = els.calls.scrollHeight;

  callTotal += 1;
  els.count.textContent = `${callTotal} call${callTotal === 1 ? "" : "s"}`;
}

function revealCall(seq) {
  const card = els.calls.querySelector(`[data-seq="${seq}"]`);
  if (!card) return;
  card.scrollIntoView({ behavior: "smooth", block: "center" });
  card.classList.add("flash");
  setTimeout(() => card.classList.remove("flash"), 1400);
}

async function pollEvents() {
  try {
    const res = await fetch(`/api/events?since=${eventCursor}`);
    const data = await res.json();
    eventCursor = data.cursor;
    for (const event of data.events) {
      renderCall(event);
      pending.push(event);
      if (event.alert) showAlert(event.result.message);
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

function shareLocation() {
  // Best-effort and silent: find_nearby_stops just won't work without it,
  // which the agent already handles as a normal ok:false reason.
  if (!navigator.geolocation) return;
  navigator.geolocation.getCurrentPosition(
    (pos) => {
      fetch("/api/location", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ lat: pos.coords.latitude, lon: pos.coords.longitude }),
      }).catch(() => {});
    },
    () => {},
    { timeout: 8000 }
  );
}

// --------------------------------------------------------------- session

async function start() {
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

function setToolsOpen(open) {
  document.body.classList.toggle("tools-open", open);
  toolsToggle.setAttribute("aria-expanded", String(open));
}

toolsToggle.addEventListener("click", () => setToolsOpen(!document.body.classList.contains("tools-open")));
toolsBackdrop.addEventListener("click", () => setToolsOpen(false)); // tap-outside-to-close, mobile sheet only
setToolsOpen(window.innerWidth > 880); // open by default on desktop, closed on mobile

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
    infoEls.body.textContent = "Could not load demo data. Is the bus arrival API running?";
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
