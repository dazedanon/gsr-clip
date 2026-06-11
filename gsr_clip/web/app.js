"use strict";

const $ = (s) => document.querySelector(s);
const video = $("#video");
const timeline = $("#timeline");

let cfg = { highlight_pre: 10, highlight_post: 2, presets: [{ id: "copy", label: "Lossless", target_mb: null }] };
let current = null;        // current session
let dur = 0;               // video duration
let inPoint = 0;
let outPoint = 0;
let dragging = null;       // 'in' | 'out' | null
let lastLabel = null;      // for export naming
let rafSeek = null;

// ---------- helpers ----------
function fmtTime(s) {
  s = Math.max(0, s || 0);
  const m = Math.floor(s / 60);
  return `${m}:${(s % 60).toFixed(1).padStart(4, "0")}`;
}
function fmtSize(b) {
  const mb = b / (1024 * 1024);
  return mb >= 1024 ? `${(mb / 1024).toFixed(1)} GB` : `${mb.toFixed(0)} MB`;
}
function escapeHtml(str) { const d = document.createElement("div"); d.textContent = str; return d.innerHTML; }
function pct(t) { return dur ? (t / dur) * 100 : 0; }
function clamp(v, lo, hi) { return Math.max(lo, Math.min(hi, v)); }

function setStatus(msg, kind) {
  const el = $("#status");
  el.className = "status" + (kind ? " " + kind : "");
  el.innerHTML = msg;
}

function liveSeek(t) {
  // throttle seeks to one per frame so dragging shows frames smoothly
  const target = clamp(t, 0, dur);
  if (typeof video.fastSeek === "function") { video.fastSeek(target); return; }
  if (rafSeek) return;
  rafSeek = requestAnimationFrame(() => { rafSeek = null; video.currentTime = target; });
}

// ---------- sessions ----------
async function loadConfig() {
  try { cfg = await (await fetch("/api/config")).json(); } catch (e) {}
  const sel = $("#preset");
  sel.innerHTML = "";
  cfg.presets.forEach((p) => {
    const o = document.createElement("option");
    o.value = p.id; o.textContent = p.label; sel.appendChild(o);
  });
  sel.addEventListener("change", updateEstimate);
}

async function loadSessions() {
  const sessions = await (await fetch("/api/sessions")).json();
  const list = $("#session-list");
  list.innerHTML = "";
  if (!sessions.length) { list.innerHTML = '<div class="field">No sessions yet.</div>'; return; }
  for (const s of sessions) {
    const el = document.createElement("div");
    el.className = "session";
    const date = new Date(s.mtime * 1000).toLocaleString();
    el.innerHTML = `<div class="game">${escapeHtml(s.game)}</div>
      <div class="sub"><span>${date}</span><span class="badge">${s.highlights.length} \u2605</span></div>
      <div class="sub"><span>${fmtSize(s.size)}</span></div>`;
    el.addEventListener("click", () => selectSession(s, el));
    list.appendChild(el);
  }
}

function selectSession(s, el) {
  current = s;
  document.querySelectorAll(".session").forEach((n) => n.classList.remove("active"));
  el.classList.add("active");
  $("#empty").classList.add("hidden");
  $("#player-wrap").classList.remove("hidden");
  $("#session-title").textContent = s.game;
  $("#session-meta").textContent = `${s.highlights.length} highlight(s) \u00b7 ${fmtSize(s.size)}`;
  lastLabel = null;
  setStatus("");
  video.src = `/video/${encodeURIComponent(s.name)}`;
  video.addEventListener("loadedmetadata", () => {
    dur = video.duration || 0;
    inPoint = 0;
    outPoint = dur;
    renderMarkers();
    render();
  }, { once: true });
}

function renderMarkers() {
  const wrap = $("#markers");
  wrap.innerHTML = "";
  current.highlights.forEach((h, i) => {
    const m = document.createElement("div");
    m.className = "marker";
    m.style.left = `${pct(h.time)}%`;
    m.title = `${h.label} @ ${fmtTime(h.time)} \u2014 click to set ${cfg.highlight_pre}s-before window`;
    m.addEventListener("click", (e) => {
      e.stopPropagation();
      inPoint = clamp(h.time - cfg.highlight_pre, 0, dur);
      outPoint = clamp(h.time + cfg.highlight_post, 0, dur);
      lastLabel = `h${i + 1}`;
      video.currentTime = h.time;
      render();
    });
    wrap.appendChild(m);
  });
}

function render() {
  $("#selection").style.left = pct(inPoint) + "%";
  $("#selection").style.width = (pct(outPoint) - pct(inPoint)) + "%";
  $("#handle-in").style.left = pct(inPoint) + "%";
  $("#handle-out").style.left = pct(outPoint) + "%";
  $("#playhead").style.left = pct(video.currentTime || 0) + "%";
  $("#in-val").textContent = fmtTime(inPoint);
  $("#out-val").textContent = fmtTime(outPoint);
  $("#sel-dur").textContent = `selection ${(outPoint - inPoint).toFixed(1)}s`;
  updateEstimate();
}

function updateEstimate() {
  const sel = Math.max(0, outPoint - inPoint);
  const preset = $("#preset").value;
  const p = cfg.presets.find((x) => x.id === preset);
  let est;
  if (p && p.target_mb != null) est = `~${p.target_mb} MB`;
  else if (current && dur) est = `~${fmtSize((current.size / dur) * sel)}`;
  else est = "";
  $("#est").textContent = est ? `${est}` : "";
}

// ---------- timeline interaction ----------
function timeFromEvent(e) {
  const r = timeline.getBoundingClientRect();
  return clamp(((e.clientX - r.left) / r.width) * dur, 0, dur);
}

function startDrag(which) {
  return (e) => {
    e.preventDefault();
    e.stopPropagation();
    dragging = which;
    video.pause();
  };
}
$("#handle-in").addEventListener("pointerdown", startDrag("in"));
$("#handle-out").addEventListener("pointerdown", startDrag("out"));

window.addEventListener("pointermove", (e) => {
  if (!dragging || !dur) return;
  const t = timeFromEvent(e);
  if (dragging === "in") inPoint = clamp(t, 0, outPoint);
  else outPoint = clamp(t, inPoint, dur);
  lastLabel = null;
  liveSeek(t);     // live frame preview at the handle
  render();
});
window.addEventListener("pointerup", () => {
  if (!dragging) return;
  const t = dragging === "in" ? inPoint : outPoint;
  video.currentTime = t;   // settle exact frame
  dragging = null;
  render();
});

// click track to scrub
timeline.addEventListener("click", (e) => {
  if (dragging) return;
  if (e.target.classList.contains("handle") || e.target.classList.contains("marker")) return;
  video.currentTime = timeFromEvent(e);
  render();
});

video.addEventListener("timeupdate", () => {
  $("#playhead").style.left = pct(video.currentTime || 0) + "%";
});

// ---------- controls ----------
$("#play").addEventListener("click", () => { video.paused ? video.play() : video.pause(); });
$("#set-in").addEventListener("click", () => { inPoint = clamp(video.currentTime, 0, outPoint); lastLabel = null; render(); });
$("#set-out").addEventListener("click", () => { outPoint = clamp(video.currentTime, inPoint, dur); lastLabel = null; render(); });
$("#export").addEventListener("click", doExport);

async function doExport() {
  if (!current) return;
  if (outPoint <= inPoint) { setStatus("Out must be after In.", "err"); return; }
  const preset = $("#preset").value;
  setStatus("Exporting\u2026", "busy");
  const res = await fetch("/api/trim", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ file: current.name, start: inPoint, end: outPoint, preset, label: lastLabel || undefined }),
  });
  const data = await res.json();
  if (data.ok) setStatus(`Exported <b>${escapeHtml(data.name)}</b> \u2192 clips/`, "ok");
  else setStatus("Export failed: " + escapeHtml(data.error || "unknown"), "err");
}

document.addEventListener("keydown", (e) => {
  if (!current || e.target.tagName === "INPUT" || e.target.tagName === "SELECT") return;
  if (e.key === "i") { inPoint = clamp(video.currentTime, 0, outPoint); lastLabel = null; render(); }
  else if (e.key === "o") { outPoint = clamp(video.currentTime, inPoint, dur); lastLabel = null; render(); }
  else if (e.key === "e") doExport();
  else if (e.key === " ") { e.preventDefault(); video.paused ? video.play() : video.pause(); }
});

loadConfig().then(loadSessions);
