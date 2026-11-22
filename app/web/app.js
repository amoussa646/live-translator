"use strict";

const LANGS = { en: "English", ru: "Русский — Russian", ar: "العربية — Arabic", de: "Deutsch — German", fr: "Français — French" };
const $ = (id) => document.getElementById(id);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const store = {
  get(k, d) { try { const v = localStorage.getItem(k); return v === null ? d : v; } catch { return d; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch { /* private mode */ } },
};
const wsBase = `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}`;
const isMobile = /Mobi|Android|iPhone|iPad/i.test(navigator.userAgent);

let ws = null, audioCtx = null, workletNode = null, mediaStream = null;
let state = "idle"; // idle | live | file | stopping | watch
let fileDuration = 0;
const rows = new Map(); // "session:id" -> {src, tgt, start, end, el}
const order = [];

// ---------- UI helpers ----------
function setStatus(text, kind = "") { const el = $("status"); el.textContent = text; el.className = "status " + kind; }
function esc(s) { return String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])); }
function fmt(t) { t = Math.max(0, Math.round(t)); return `${Math.floor(t / 60)}:${String(t % 60).padStart(2, "0")}`; }
function nearBottom() { return window.innerHeight + window.scrollY >= document.body.scrollHeight - 160; }
function scrollDown(force) { if (force) window.scrollTo({ top: document.body.scrollHeight }); }

function updateUI() {
  const start = $("start");
  start.disabled = state === "stopping" || state === "watch";
  start.textContent = state === "live" || state === "file" ? "Stop" : state === "stopping" ? "Finishing…" : "Start";
  start.classList.toggle("stop", state === "live" || state === "file");
  $("fileBtn").classList.toggle("disabled", state !== "idle");
  $("device").disabled = state !== "idle";
  $("watch").textContent = state === "watch" ? "Stop watching" : "Watch";
  $("watch").disabled = !(state === "idle" || state === "watch");
  $("main").classList.toggle("single", $("tgt").value === "none");
  document.querySelector(".row.live").classList.toggle("idle", state === "idle");
  $("empty").hidden = order.length > 0 || state !== "idle";
  if (state === "idle") $("level").style.width = "0";
}

function fillLanguages() {
  const src = $("src"), tgt = $("tgt");
  src.add(new Option("Auto-detect", "auto"));
  for (const [c, n] of Object.entries(LANGS)) { src.add(new Option(n, c)); tgt.add(new Option(n, c)); }
  tgt.add(new Option("— transcript only —", "none"));
  src.value = store.get("src", "en");
  tgt.value = store.get("tgt", "ru");
}

async function listDevices() {
  const sel = $("device");
  const prev = store.get("device", "default");
  sel.innerHTML = "";
  if (!navigator.mediaDevices) {
    sel.add(new Option("No microphone access (needs https)", ""));
    return;
  }
  let mics = [];
  try { mics = (await navigator.mediaDevices.enumerateDevices()).filter((d) => d.kind === "audioinput"); } catch { /* ignore */ }
  if (!mics.length || mics.every((d) => !d.deviceId)) sel.add(new Option("Microphone (default)", "default"));
  else mics.forEach((d, i) => sel.add(new Option(d.label || `Microphone ${i + 1}`, d.deviceId)));
  if (navigator.mediaDevices.getDisplayMedia && !isMobile) {
    sel.add(new Option("Tab / system audio — pick a tab or screen to share", "__display__"));
  }
  if ([...sel.options].some((o) => o.value === prev)) sel.value = prev;
}

async function getStream(dev) {
  if (!navigator.mediaDevices) throw new Error("This page needs https for microphone access. Use the https link.");
  if (dev === "__display__") {
    const s = await navigator.mediaDevices.getDisplayMedia({
      video: true, audio: { echoCancellation: false, noiseSuppression: false, autoGainControl: false },
      systemAudio: "include", selfBrowserSurface: "exclude",
    });
    if (!s.getAudioTracks().length) { s.getTracks().forEach((t) => t.stop()); throw new Error("No audio was shared — tick “Share audio” in the picker."); }
    s.getVideoTracks().forEach((t) => t.stop());
    return new MediaStream(s.getAudioTracks());
  }
  const audio = { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 };
  if (dev && dev !== "default") audio.deviceId = { exact: dev };
  return navigator.mediaDevices.getUserMedia({ audio });
}

// ---------- transcript rendering ----------
function addRow(m) {
  const key = `${m.session}:${m.id}`;
  if (rows.has(key)) return;
  const stick = nearBottom();
  const el = document.createElement("div");
  el.className = "row";
  el.innerHTML = `<div class="src" dir="auto" lang="${esc(m.lang)}">${esc(m.text)}</div><div class="tgt muted" dir="auto">…</div>`;
  $("rows").appendChild(el);
  rows.set(key, { src: m.text, tgt: null, start: m.start || 0, end: m.end || 0, el });
  order.push(key);
  updateUI();
  scrollDown(stick);
}

function setTranslation(m) {
  const r = rows.get(`${m.session}:${m.id}`);
  if (!r) return;
  const cell = r.el.querySelector(".tgt");
  r.tgt = m.text;
  if (m.text == null) { cell.textContent = ""; return; }
  cell.classList.remove("muted");
  cell.setAttribute("lang", m.target || "");
  cell.innerHTML = `${esc(m.text)}${m.ms != null ? `<span class="meta">${(m.ms / 1000).toFixed(1)} s</span>` : ""}`;
}

function renderLive(m) {
  const stick = nearBottom();
  const pending = (m.pending || "").trim(), buf = (m.buffer || "").trim();
  $("liveSrc").innerHTML = `${esc(pending)}${pending && buf ? " " : ""}<span class="buf">${esc(buf)}</span>`;
  $("liveTgt").textContent = pending || buf ? "…" : "";
  scrollDown(stick);
}

function handle(m) {
  switch (m.type) {
    case "asr":
      renderLive(m);
      if ((state === "file" || state === "stopping") && fileDuration) setStatus(`Transcribing file… ${fmt(m.t)} / ${fmt(fileDuration)}`);
      break;
    case "sentence": addRow(m); break;
    case "translation": setTranslation(m); break;
    case "error": setStatus("Engine error: " + m.error, "err"); break;
    case "done":
      if (state === "watch") { renderLive({}); break; }
      finish(fileDuration ? "File finished. Export with TXT or SRT." : "Stopped.");
      break;
    default: break;
  }
}

// ---------- connection ----------
function openSocket(path = "/ws") {
  return new Promise((resolve, reject) => {
    const q = new URLSearchParams({ source: $("src").value, target: $("tgt").value });
    const sock = new WebSocket(`${wsBase}${path}?${q}`);
    sock.binaryType = "arraybuffer";
    let opened = false;
    sock.onopen = () => { opened = true; ws = sock; resolve(); };
    sock.onerror = () => { if (!opened) reject(new Error("Can't reach the translator engine on the Mac.")); };
    sock.onmessage = (e) => { try { handle(JSON.parse(e.data)); } catch (err) { console.error(err); } };
    sock.onclose = () => {
      if (ws !== sock) return;
      ws = null;
      if (state !== "idle") finish(state === "stopping" ? "Done." : "Connection to the engine closed.");
    };
  });
}

function sendConfig() {
  store.set("src", $("src").value);
  store.set("tgt", $("tgt").value);
  updateUI();
  if (ws && ws.readyState === 1 && (state === "live" || state === "file")) {
    ws.send(JSON.stringify({ type: "config", source: $("src").value, target: $("tgt").value }));
  }
}

function stopAudio() {
  if (workletNode) { workletNode.port.onmessage = null; workletNode.disconnect(); workletNode = null; }
  if (audioCtx) { audioCtx.close().catch(() => {}); audioCtx = null; }
  if (mediaStream) { mediaStream.getTracks().forEach((t) => t.stop()); mediaStream = null; }
  $("level").style.width = "0";
}

function finish(msg) {
  stopAudio();
  if (ws) { const s = ws; ws = null; try { s.close(); } catch { /* ignore */ } }
  state = "idle";
  renderLive({});
  updateUI();
  if (msg) setStatus(msg);
}

// ---------- live audio ----------
async function startLive() {
  const dev = $("device").value;
  store.set("device", dev);
  setStatus("Asking for audio access…");
  try { mediaStream = await getStream(dev); }
  catch (e) { setStatus("Audio input: " + (e.message || e.name), "err"); return; }
  listDevices(); // device names appear once permission is granted
  try { await openSocket(); }
  catch (e) { stopAudio(); setStatus(e.message, "err"); return; }
  try {
    audioCtx = new AudioContext({ sampleRate: 16000 });
    await audioCtx.audioWorklet.addModule("/pcm-worklet.js");
    const src = audioCtx.createMediaStreamSource(mediaStream);
    workletNode = new AudioWorkletNode(audioCtx, "pcm16", {
      numberOfInputs: 1, numberOfOutputs: 0, channelCount: 1, channelCountMode: "explicit", channelInterpretation: "speakers",
    });
    workletNode.port.onmessage = (e) => {
      if (ws && ws.readyState === 1) ws.send(e.data.pcm);
      $("level").style.width = `${Math.min(100, Math.round(e.data.level * 400))}%`;
    };
    src.connect(workletNode);
  } catch (e) { finish(); setStatus("Audio setup failed: " + e.message, "err"); return; }
  mediaStream.getAudioTracks()[0].addEventListener("ended", () => { if (state === "live") stop(); });
  fileDuration = 0;
  state = "live";
  updateUI();
  setStatus(`Listening — ${$("src").selectedOptions[0].text} → ${$("tgt").selectedOptions[0].text}`, "ok");
}

function stop() {
  if (state === "watch") { finish("Stopped watching."); return; }
  if (state !== "live" && state !== "file") return;
  state = "stopping";
  stopAudio();
  updateUI();
  if (ws && ws.readyState === 1) { ws.send(new ArrayBuffer(0)); setStatus("Finishing the last sentence…"); }
  else finish("Stopped.");
}

// ---------- audio files ----------
async function runFile(file) {
  if (!file || state !== "idle") return;
  setStatus(`Decoding ${file.name}…`);
  let audio;
  try {
    const ctx = new AudioContext({ sampleRate: 16000 });
    audio = await ctx.decodeAudioData(await file.arrayBuffer());
    ctx.close();
  } catch { setStatus("This browser can't decode that file. Try mp3, wav, m4a, ogg or flac.", "err"); return; }
  const n = audio.length, chs = audio.numberOfChannels, mono = new Float32Array(n);
  for (let c = 0; c < chs; c++) { const d = audio.getChannelData(c); for (let i = 0; i < n; i++) mono[i] += d[i] / chs; }
  fileDuration = n / 16000;
  try { await openSocket(); } catch (e) { setStatus(e.message, "err"); return; }
  state = "file";
  updateUI();
  setStatus(`Sending ${file.name} (${fmt(fileDuration)})…`, "ok");
  const CH = 8000; // 0.5 s per message; the engine paces us through TCP backpressure
  for (let i = 0; i < n; i += CH) {
    if (state !== "file" || !ws || ws.readyState !== 1) break;
    const s = mono.subarray(i, Math.min(n, i + CH));
    const pcm = new Int16Array(s.length);
    for (let j = 0; j < s.length; j++) { const v = Math.max(-1, Math.min(1, s[j])); pcm[j] = v < 0 ? v * 0x8000 : v * 0x7fff; }
    while (ws && ws.bufferedAmount > 512 * 1024) await sleep(25);
    if (!ws) break;
    ws.send(pcm.buffer);
  }
  if (state === "file" && ws && ws.readyState === 1) { state = "stopping"; updateUI(); ws.send(new ArrayBuffer(0)); }
}

// ---------- watch mode ----------
async function toggleWatch() {
  if (state === "watch") { stop(); return; }
  if (state !== "idle") return;
  try { await openSocket("/ws/watch"); } catch (e) { setStatus(e.message, "err"); return; }
  state = "watch";
  updateUI();
  setStatus("Watching — sentences from any live session on the Mac appear here.", "ok");
}

// ---------- export ----------
function download(name, text) {
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([text], { type: "text/plain;charset=utf-8" }));
  a.download = name;
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 5000);
}
function srtTime(s) {
  const ms = Math.max(0, Math.round(s * 1000)), p = (x, n = 2) => String(x).padStart(n, "0");
  return `${p(Math.floor(ms / 3600000))}:${p(Math.floor(ms / 60000) % 60)}:${p(Math.floor(ms / 1000) % 60)},${p(ms % 1000, 3)}`;
}
function exportAs(kind) {
  const list = order.map((k) => rows.get(k));
  if (!list.length) { setStatus("Nothing to export yet."); return; }
  const stamp = new Date().toISOString().slice(0, 16).replace(/[:T]/g, "-");
  if (kind === "txt") {
    download(`translation-${stamp}.txt`, list.map((r) => (r.tgt ? `${r.src}\n${r.tgt}` : r.src)).join("\n\n") + "\n");
  } else {
    download(`translation-${stamp}.srt`, list.map((r, i) =>
      `${i + 1}\n${srtTime(r.start)} --> ${srtTime(Math.max(r.end, r.start + 1))}\n${r.src}${r.tgt ? "\n" + r.tgt : ""}\n`).join("\n"));
  }
}

// ---------- startup ----------
async function pollStatus() {
  for (;;) {
    try {
      const s = await (await fetch("/api/status")).json();
      if (s.lan_url) { $("lan").hidden = false; $("lan").innerHTML = `Other devices on this Wi-Fi: <code>${esc(s.lan_url)}</code>`; }
      if (s.ready) { if (state === "idle") setStatus("Engine ready. Everything runs on this Mac — nothing goes online.", "ok"); return; }
      setStatus("Loading models…");
    } catch { setStatus("Engine not reachable — is run.sh running on the Mac?", "err"); }
    await sleep(2000);
  }
}

function init() {
  fillLanguages();
  listDevices();
  if (navigator.mediaDevices && navigator.mediaDevices.addEventListener) navigator.mediaDevices.addEventListener("devicechange", listDevices);
  const fs = Number(store.get("fs", "18"));
  document.documentElement.style.setProperty("--fs", `${fs}px`);
  const bump = (d) => {
    const v = Math.min(40, Math.max(13, Number(store.get("fs", "18")) + d));
    store.set("fs", String(v)); document.documentElement.style.setProperty("--fs", `${v}px`);
  };
  $("smaller").onclick = () => bump(-2);
  $("bigger").onclick = () => bump(2);
  $("src").onchange = sendConfig;
  $("tgt").onchange = sendConfig;
  $("swap").onclick = () => {
    const s = $("src").value, t = $("tgt").value;
    if (s === "auto" || t === "none") return;
    $("src").value = t; $("tgt").value = s; sendConfig();
  };
  $("device").onchange = () => store.set("device", $("device").value);
  $("start").onclick = () => (state === "idle" ? startLive() : stop());
  $("file").onchange = (e) => { const f = e.target.files[0]; e.target.value = ""; runFile(f); };
  $("watch").onclick = toggleWatch;
  $("exportTxt").onclick = () => exportAs("txt");
  $("exportSrt").onclick = () => exportAs("srt");
  $("clear").onclick = () => { rows.clear(); order.length = 0; $("rows").innerHTML = ""; updateUI(); };
  const main = $("main");
  main.addEventListener("dragover", (e) => { e.preventDefault(); main.classList.add("drag"); });
  main.addEventListener("dragleave", () => main.classList.remove("drag"));
  main.addEventListener("drop", (e) => { e.preventDefault(); main.classList.remove("drag"); runFile(e.dataTransfer.files[0]); });
  updateUI();
  pollStatus();
}

init();
