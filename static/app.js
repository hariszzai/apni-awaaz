// Apni Awaaz web client: landing, profile and voice setup, conversation room, push-to-talk, playback.

import { createParticleField } from "./particles.js";

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];

const VOICE_SECONDS = 20;
const MIN_TURN_SECONDS = 0.5;
const STT_SAMPLE_RATE = 16000;

// ---------- storage (per-browser conveniences; the app still works without it) ----------

function makeStore(area) {
  return {
    get(key) {
      try { return JSON.parse(area().getItem(key)); } catch { return null; }
    },
    set(key, value) {
      try { area().setItem(key, JSON.stringify(value)); } catch { /* private mode */ }
    },
    remove(key) {
      try { area().removeItem(key); } catch { /* private mode */ }
    },
  };
}
const store = makeStore(() => localStorage);
const tabStore = makeStore(() => sessionStorage);

function randomHex(bytes) {
  return [...crypto.getRandomValues(new Uint8Array(bytes))].map((b) => b.toString(16).padStart(2, "0")).join("");
}

function storedId(storage, key, bytes) {
  let id = storage.get(key);
  if (typeof id !== "string" || !new RegExp(`^[a-f0-9]{${bytes * 2}}$`).test(id)) {
    id = randomHex(bytes);
    storage.set(key, id);
  }
  return id;
}

// ---------- audio: capture to 16 kHz WAV (Prisma doesn't accept the WebM phones record) ----------

function downsample(samples, fromRate, toRate) {
  if (fromRate === toRate) return samples;
  const ratio = fromRate / toRate;
  const out = new Float32Array(Math.floor(samples.length / ratio));
  for (let i = 0; i < out.length; i++) {
    const start = Math.floor(i * ratio);
    const end = Math.min(Math.floor((i + 1) * ratio), samples.length);
    let sum = 0;
    for (let j = start; j < end; j++) sum += samples[j];
    out[i] = sum / Math.max(1, end - start);
  }
  return out;
}

function encodeWav(samples, sampleRate) {
  const buffer = new ArrayBuffer(44 + samples.length * 2);
  const view = new DataView(buffer);
  const text = (offset, s) => [...s].forEach((c, i) => view.setUint8(offset + i, c.charCodeAt(0)));
  text(0, "RIFF");
  view.setUint32(4, 36 + samples.length * 2, true);
  text(8, "WAVE");
  text(12, "fmt ");
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true); // PCM
  view.setUint16(22, 1, true); // mono
  view.setUint32(24, sampleRate, true);
  view.setUint32(28, sampleRate * 2, true);
  view.setUint16(32, 2, true);
  view.setUint16(34, 16, true);
  text(36, "data");
  view.setUint32(40, samples.length * 2, true);
  for (let i = 0; i < samples.length; i++) {
    const s = Math.max(-1, Math.min(1, samples[i]));
    view.setInt16(44 + i * 2, s < 0 ? s * 0x8000 : s * 0x7fff, true);
  }
  return buffer;
}

// Turns mic blocks at the device rate into 16 kHz 16-bit PCM for streaming, carrying
// leftover samples between blocks so nothing is lost or doubled at the edges.
class StreamResampler {
  constructor(fromRate) {
    this.ratio = fromRate / STT_SAMPLE_RATE;
    this.leftover = new Float32Array(0);
  }

  push(block) {
    const input = new Float32Array(this.leftover.length + block.length);
    input.set(this.leftover);
    input.set(block, this.leftover.length);
    const count = Math.floor(input.length / this.ratio);
    const out = new Int16Array(count);
    for (let i = 0; i < count; i++) {
      const start = Math.floor(i * this.ratio);
      const end = Math.floor((i + 1) * this.ratio);
      let sum = 0;
      for (let j = start; j < end; j++) sum += input[j];
      const s = Math.max(-1, Math.min(1, sum / Math.max(1, end - start)));
      out[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
    }
    this.leftover = input.slice(Math.floor(count * this.ratio));
    return out;
  }
}

class Recorder {
  constructor(ctx, stream) {
    this.ctx = ctx;
    this.node = new AudioWorkletNode(ctx, "capture");
    ctx.createMediaStreamSource(stream).connect(this.node);
    // Keep the node in the graph so it is processed, without playing the mic back
    const mute = ctx.createGain();
    mute.gain.value = 0;
    this.node.connect(mute).connect(ctx.destination);
    this.blocks = [];
    this.onBlock = null;
    this.onDone = null;
    this.node.port.onmessage = (event) => {
      if (event.data === "done") this.onDone?.();
      else if (this.onBlock) this.onBlock(event.data);
      else this.blocks.push(event.data);
    };
  }

  // onBlock: stream each block as it arrives instead of keeping it for a WAV
  start(onBlock = null) {
    this.blocks = [];
    this.onBlock = onBlock;
    this.startedAt = performance.now();
    this.node.port.postMessage("start");
  }

  get seconds() {
    return (performance.now() - this.startedAt) / 1000;
  }

  async stop() {
    const done = new Promise((resolve) => { this.onDone = resolve; });
    this.node.port.postMessage("stop");
    await done;
    if (this.onBlock) {
      this.onBlock = null;
      return null;
    }
    const total = this.blocks.reduce((n, b) => n + b.length, 0);
    const all = new Float32Array(total);
    let offset = 0;
    for (const block of this.blocks) {
      all.set(block, offset);
      offset += block.length;
    }
    return encodeWav(downsample(all, this.ctx.sampleRate, STT_SAMPLE_RATE), STT_SAMPLE_RATE);
  }
}

// Plays raw 16-bit PCM chunks back to back as they arrive from the server.
class Player {
  constructor(ctx) {
    this.ctx = ctx;
    this.rate = 44100;
    this.next = 0;
    this.carry = null;
  }

  begin(rate) {
    this.rate = rate;
    this.carry = null;
    this.next = Math.max(this.next, this.ctx.currentTime + 0.15);
  }

  push(data) {
    let bytes = new Uint8Array(data);
    if (this.carry) {
      const merged = new Uint8Array(this.carry.length + bytes.length);
      merged.set(this.carry);
      merged.set(bytes, this.carry.length);
      bytes = merged;
      this.carry = null;
    }
    if (bytes.length % 2) {
      this.carry = bytes.slice(-1);
      bytes = bytes.slice(0, -1);
    }
    if (!bytes.length) return;
    const pcm = new Int16Array(bytes.buffer, bytes.byteOffset, bytes.length / 2);
    const floats = new Float32Array(pcm.length);
    for (let i = 0; i < pcm.length; i++) floats[i] = pcm[i] / 32768;
    field?.pulse(rms(floats) * 6); // the orb pulses as the other person's voice arrives
    const buffer = this.ctx.createBuffer(1, floats.length, this.rate);
    buffer.copyToChannel(floats, 0);
    const source = this.ctx.createBufferSource();
    source.buffer = buffer;
    source.connect(this.ctx.destination);
    const at = Math.max(this.next, this.ctx.currentTime + 0.02);
    source.start(at);
    this.next = at + buffer.duration;
  }

  // Seconds of audio still queued to play
  get remaining() {
    return Math.max(0, this.next - this.ctx.currentTime);
  }
}

// ---------- app state ----------

const userId = storedId(store, "aa_user", 16);
const clientId = storedId(tabStore, "aa_client", 8); // one per tab: two tabs are two people
let config = null;
let profile = store.get("aa_profile");
// The signed cloned voice lives in this browser only (see backend/voices.py)
let myVoice = store.get("aa_voice");
let hasVoice = Boolean(myVoice);
let afterSetup = null; // what Continue on the setup screen does next

let audioCtx = null;
let recorder = null;
let player = null;
let field = null; // live particle background
let currentSample = null; // the "Hear it" clip playing now

let roomId = null;
let socket = null;
let myPid = null;
let leaving = false;
let participants = [];
let talking = false;
let otherSpeaking = null; // name of the other person while they hold their button
let talkTimer = null;
let badgeTimer = null;
const turns = new Map();

// ---------- helpers ----------

function show(view) {
  document.body.classList.remove("view-landing", "view-setup", "view-room");
  document.body.classList.add(`view-${view}`);
  for (const screen of $$(".screen")) screen.hidden = screen.id !== `screen-${view}`;
  closeMenu();
  closeSheet();
  stopSample();
  window.scrollTo(0, 0);
  document.body.classList.remove("scrolled"); // no scroll event fires if we were already at the top
  if (view === "landing") field?.setFormation("orb");
  else field?.setFormation(view); // a faint orb behind setup; a small one above the room
}

let toastTimer = null;
function toast(message) {
  const el = $("#toast");
  el.textContent = message;
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, 4200);
}

function languageName(code) {
  return config.languages[code] || code;
}

// Loudness of a block of samples, roughly 0 (silence) to 0.3 (loud speech)
function rms(samples) {
  let sum = 0;
  let count = 0;
  for (let i = 0; i < samples.length; i += 4) {
    sum += samples[i] * samples[i];
    count++;
  }
  return Math.sqrt(sum / Math.max(1, count));
}

function stripTags(text) {
  return (text || "").replace(/<[a-z ]+>/gi, "").replace(/\s+/g, " ").trim();
}

function inviteCode() {
  const code = new URLSearchParams(location.search).get("room");
  return code && /^[A-Za-z0-9]{6}$/.test(code) ? code.toUpperCase() : null;
}

async function ensureAudio() {
  if (!audioCtx) {
    audioCtx = new AudioContext();
    await audioCtx.audioWorklet.addModule("/static/recorder-worklet.js");
    player = new Player(audioCtx);
  }
  if (audioCtx.state === "suspended") await audioCtx.resume();
}

async function ensureMic() {
  if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia) {
    throw new Error("The microphone needs a secure link. Open the https:// address the server printed.");
  }
  await ensureAudio();
  if (recorder) return;
  const stream = await navigator.mediaDevices.getUserMedia({
    audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true },
  });
  recorder = new Recorder(audioCtx, stream);
}

function micError(error) {
  if (error?.name === "NotAllowedError") return "Allow microphone access to talk.";
  if (error?.name === "NotFoundError") return "No microphone found on this device.";
  return error?.message || "Couldn't start the microphone.";
}

// ---------- menu (phones) and info sheets ----------

const burger = $(".burger");

function closeMenu() {
  document.body.classList.remove("menu-open");
  burger.setAttribute("aria-expanded", "false");
  burger.setAttribute("aria-label", "Open menu");
}

burger.addEventListener("click", () => {
  const open = !document.body.classList.contains("menu-open");
  document.body.classList.toggle("menu-open", open);
  burger.setAttribute("aria-expanded", String(open));
  burger.setAttribute("aria-label", open ? "Close menu" : "Open menu");
});
$(".menu-backdrop").addEventListener("click", closeMenu);
matchMedia("(min-width: 901px)").addEventListener("change", (e) => { if (e.matches) closeMenu(); });

function openSheet() {
  closeMenu();
  $(".sheet").hidden = false;
  $(".sheet-backdrop").hidden = false;
  $("#join-code").focus();
}

function closeSheet() {
  $(".sheet").hidden = true;
  $(".sheet-backdrop").hidden = true;
}

$(".sheet-close").addEventListener("click", closeSheet);
$(".sheet-backdrop").addEventListener("click", closeSheet);
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  closeSheet();
  closeMenu();
});
for (const button of $$("[data-sheet]")) {
  button.addEventListener("click", openSheet);
}

// Nav links scroll to a landing section (coming back to the landing first if needed)
for (const button of $$("[data-scroll]")) {
  button.addEventListener("click", () => {
    closeMenu();
    if (!document.body.classList.contains("view-landing")) {
      if (roomId) leaveRoom();
      else goHome();
    }
    document.getElementById(button.dataset.scroll)?.scrollIntoView({ behavior: "smooth", block: "start" });
  });
}

// ---------- landing ----------

function renderLanding() {
  const code = inviteCode();
  $("#badge-text").textContent = code ? "You're invited to a conversation" : "Built on Gnani AI · Prisma + Timbre";
  $("#lede").textContent = code
    ? "Someone wants to talk to you. Speak your language, and hear them in yours."
    : "Live voice translation across 10 Indian languages. They hear your words in their language, spoken in your own cloned voice.";
  $("#cta-primary").textContent = code ? "Join conversation" : "Start a conversation";
  $(".header-cta").textContent = code ? "Join" : "Start talking";
  $("#cta-voice").textContent = hasVoice ? "Change my voice" : "Set up my voice";
}

function goHome() {
  renderLanding();
  show("landing");
}

// Start or join; sends people through setup first if they haven't introduced themselves
function primaryAction() {
  const code = inviteCode();
  const action = code ? () => joinRoom(code) : startRoom;
  if (!profile?.name) {
    afterSetup = action;
    openSetup();
    return;
  }
  action();
}

for (const el of $$("[data-action]")) {
  el.addEventListener("click", () => {
    const action = el.dataset.action;
    if (action === "primary") primaryAction();
    else if (action === "voice") { afterSetup = null; openSetup(); }
    else if (action === "home") { afterSetup = null; goHome(); }
  });
}

$("#logo").addEventListener("click", (e) => {
  e.preventDefault();
  if (roomId) leaveRoom();
  else goHome();
});

$("#join-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const code = $("#join-code").value.trim().toUpperCase();
  if (!/^[A-Z0-9]{6}$/.test(code)) {
    toast("Codes are 6 letters or numbers.");
    return;
  }
  closeSheet();
  if (!profile?.name) {
    afterSetup = () => joinRoom(code);
    openSetup();
    return;
  }
  joinRoom(code);
});

// ---------- setup screen ----------

function fillSelect(select, options, selected) {
  select.replaceChildren(...Object.entries(options).map(([value, label]) => {
    const option = new Option(label, value);
    option.selected = value === selected;
    return option;
  }));
}

let chosenVoice = "Shubham"; // the Expressive voice picked on this screen
let pendingRecording = null; // a recording waiting for "Use this recording"
let rerecording = false;

function setMyVoice(voice) {
  myVoice = voice;
  hasVoice = Boolean(voice);
  if (voice) store.set("aa_voice", voice);
  else store.remove("aa_voice");
}

function renderVoiceStatus() {
  const pill = $("#voice-pill");
  pill.textContent = hasVoice ? "Ready" : "Not set up";
  pill.classList.toggle("ready", hasVoice);
  $("#voice-status").textContent = hasVoice
    ? "People hear your translated words in your own voice. It's kept only in this browser."
    : "Without it, people hear an expressive AI voice instead. You can set it up any time.";
  const recording = !hasVoice || rerecording;
  $("#voice-ready").hidden = recording;
  $("#voice-setup").hidden = !recording;
  $("#cancel-rerecord").hidden = !(hasVoice && rerecording);
  $("#voice-review").hidden = !pendingRecording;
  $("#record-voice").hidden = Boolean(pendingRecording);
}

function renderVoiceGrid() {
  $("#voice-grid").replaceChildren(...Object.entries(config.voices).map(([name, gender]) => {
    const option = document.createElement("div");
    option.className = "voice-option";
    option.dataset.voice = name;
    const pick = document.createElement("button");
    pick.type = "button";
    pick.className = "voice-pick";
    pick.setAttribute("role", "radio");
    pick.setAttribute("aria-checked", String(name === chosenVoice));
    pick.innerHTML = `<span class="voice-name"></span><small></small>`;
    pick.querySelector(".voice-name").textContent = name;
    pick.querySelector("small").textContent = gender;
    pick.addEventListener("click", () => {
      chosenVoice = name;
      for (const other of $$(".voice-pick")) other.setAttribute("aria-checked", String(other === pick));
    });
    const play = document.createElement("button");
    play.type = "button";
    play.className = "voice-play";
    play.setAttribute("aria-label", `Hear ${name}`);
    play.innerHTML = '<span class="mini-play"></span>';
    play.addEventListener("click", () => {
      const language = $("#language").value;
      playPreview(play, `stock:${name}:${language}`, () => fetch(`/api/voices/${name}/preview?language=${language}`));
    });
    option.append(pick, play);
    return option;
  }));
}

// ----- previews: one shared player; each clip is fetched once per page visit -----

const previewClips = new Map(); // key -> object URL
const previewAudio = new Audio();
let previewButton = null;

function stopPreview() {
  previewAudio.pause();
  previewButton?.classList.remove("playing", "loading");
  previewButton = null;
}

previewAudio.addEventListener("ended", stopPreview);

async function playPreview(button, key, load) {
  if (previewButton === button) {
    stopPreview();
    return;
  }
  stopPreview();
  previewButton = button;
  try {
    let url = previewClips.get(key);
    if (!url) {
      button.classList.add("loading");
      const response = await load();
      if (!response.ok) throw new Error((await response.json().catch(() => ({}))).detail || "Couldn't load the preview.");
      url = URL.createObjectURL(await response.blob());
      previewClips.set(key, url);
    }
    if (previewButton !== button) return; // another preview started while this one loaded
    button.classList.remove("loading");
    button.classList.add("playing");
    previewAudio.src = url;
    await previewAudio.play();
  } catch (error) {
    if (previewButton === button) stopPreview();
    if (error?.name !== "AbortError") toast(error.message || "Couldn't play the preview.");
  }
}

function hearClone() {
  const language = $("#language").value;
  const key = `clone:${myVoice.sig}:${language}`;
  return playPreview($("#hear-clone"), key, () => fetch("/api/voice/preview", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ voice: myVoice, language }),
  }));
}

function openSetup() {
  $("#name").value = profile?.name || "";
  fillSelect($("#language"), config.languages, profile?.language || "hi-IN");
  chosenVoice = config.voices[profile?.voice] ? profile.voice : "Shubham";
  renderVoiceGrid();
  rerecording = false;
  pendingRecording = null;
  $("#consent").checked = false;
  $("#record-voice").disabled = true;
  $("#voice-progress").textContent = "";
  $("#save-profile").textContent = afterSetup ? (inviteCode() ? "Continue and join" : "Continue and start") : "Save";
  renderVoiceStatus();
  show("setup");
}

$("#consent").addEventListener("change", (e) => { $("#record-voice").disabled = !e.target.checked; });

async function recordVoice() {
  const button = $("#record-voice");
  const progress = $("#voice-progress");
  const bar = $("#voice-bar");
  try {
    await ensureMic();
  } catch (error) {
    toast(micError(error));
    return;
  }
  stopPreview();
  pendingRecording = null;
  renderVoiceStatus();
  button.disabled = true;
  bar.parentElement.hidden = false;
  bar.style.width = "0";
  recorder.start();
  for (let elapsed = 1; elapsed <= VOICE_SECONDS; elapsed++) {
    progress.textContent = `Recording… keep talking (${VOICE_SECONDS - elapsed + 1}s)`;
    bar.style.width = `${(elapsed / VOICE_SECONDS) * 100}%`;
    await new Promise((r) => setTimeout(r, 1000));
  }
  const wav = await recorder.stop();
  bar.parentElement.hidden = true;
  button.disabled = !$("#consent").checked;
  // Listen back before cloning: a noisy or mumbled take shouldn't become your voice
  pendingRecording = new Blob([wav], { type: "audio/wav" });
  previewClips.delete("recording");
  progress.textContent = "Listen back. If it sounds clear, use it.";
  renderVoiceStatus();
}

$("#record-voice").addEventListener("click", recordVoice);
$("#record-again").addEventListener("click", recordVoice);

$("#play-recording").addEventListener("click", () => {
  if (!pendingRecording) return;
  if (!previewClips.has("recording")) previewClips.set("recording", URL.createObjectURL(pendingRecording));
  playPreview($("#play-recording"), "recording", null);
});

$("#use-recording").addEventListener("click", async () => {
  if (!pendingRecording) return;
  if (!$("#consent").checked) {
    toast("Please tick the consent box first.");
    return;
  }
  stopPreview();
  const button = $("#use-recording");
  const progress = $("#voice-progress");
  button.disabled = true;
  progress.textContent = "Creating your voice…";
  const form = new FormData();
  form.append("consent", "true");
  form.append("audio_file", pendingRecording, "voice.wav");
  try {
    const response = await fetch("/api/voice", { method: "POST", body: form });
    if (!response.ok) throw new Error((await response.json()).detail || "Voice setup failed");
    const previous = myVoice;
    setMyVoice((await response.json()).voice);
    if (previous) forgetVoice(previous); // a re-record replaces the old voice
    pendingRecording = null;
    rerecording = false;
    progress.textContent = "";
    if (profile) profile.mode = "my_voice";
    renderVoiceStatus();
    hearClone(); // the moment of truth: your voice, already speaking your language
  } catch (error) {
    progress.textContent = error.message;
  } finally {
    button.disabled = false;
  }
});

$("#hear-clone").addEventListener("click", hearClone);

$("#rerecord").addEventListener("click", () => {
  stopPreview();
  rerecording = true;
  pendingRecording = null;
  $("#consent").checked = false;
  $("#record-voice").disabled = true;
  $("#voice-progress").textContent = "Your current voice stays until you use a new recording.";
  renderVoiceStatus();
});

$("#cancel-rerecord").addEventListener("click", () => {
  rerecording = false;
  pendingRecording = null;
  $("#voice-progress").textContent = "";
  renderVoiceStatus();
});

function forgetVoice(voice) {
  // Ask the server to drop the preview clips it cached for this voice
  fetch("/api/voice/forget", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ voice }),
  }).catch(() => {});
}

$("#delete-voice").addEventListener("click", () => {
  if (!confirm("Delete your cloned voice? You can record it again later.")) return;
  stopPreview();
  forgetVoice(myVoice);
  setMyVoice(null);
  if (profile) profile.mode = "expressive";
  renderVoiceStatus();
});

$("#language").addEventListener("change", stopPreview);

$("#save-profile").addEventListener("click", () => {
  const name = $("#name").value.trim();
  if (!name) {
    toast("Please enter your name.");
    $("#name").focus();
    return;
  }
  stopPreview();
  profile = {
    name,
    language: $("#language").value,
    voice: chosenVoice,
    mode: hasVoice ? (profile?.mode || "my_voice") : "expressive",
  };
  store.set("aa_profile", profile);
  const next = afterSetup;
  afterSetup = null;
  if (next) next();
  else goHome();
});

// ---------- room ----------

async function startRoom() {
  // Mic first: no point creating a room nobody can talk in
  try {
    await ensureMic();
  } catch (error) {
    toast(micError(error));
    return;
  }
  try {
    const response = await fetch("/api/rooms", { method: "POST" });
    const { room_id } = await response.json();
    enterRoom(room_id);
  } catch {
    toast("Couldn't reach the server. Is it still running?");
  }
}

async function joinRoom(code) {
  try {
    await ensureMic();
  } catch (error) {
    toast(micError(error));
    return;
  }
  enterRoom(code);
}

function enterRoom(code) {
  roomId = code;
  leaving = false;
  myPid = null;
  participants = [];
  turns.clear();
  $("#transcript").replaceChildren();
  $("#room-code").textContent = code;
  $("#room-chip").hidden = false;
  history.replaceState(null, "", `?room=${code}`);
  show("room");
  renderRoom();
  connect();
}

function connect() {
  const protocol = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${protocol}://${location.host}/ws/${roomId}`);
  ws.binaryType = "arraybuffer";
  socket = ws;

  ws.onopen = () => {
    ws.send(JSON.stringify({
      type: "join",
      user_id: userId,
      client_id: clientId,
      name: profile.name,
      language: profile.language,
      mode: profile.mode,
      voice: profile.voice,
      cloned_voice: myVoice,
    }));
  };
  ws.onmessage = (event) => {
    if (typeof event.data !== "string") {
      player.push(event.data);
      return;
    }
    handleMessage(JSON.parse(event.data));
  };
  ws.onclose = () => {
    if (socket !== ws || leaving || !roomId) return;
    participants = [];
    renderRoom();
    $("#talk-status").textContent = "Connection lost. Reconnecting…";
    setTimeout(() => { if (socket === ws && !leaving) connect(); }, 2000);
  };
}

function leaveRoom() {
  leaving = true;
  roomId = null;
  socket?.close();
  socket = null;
  $("#room-chip").hidden = true;
  history.replaceState(null, "", "/");
  goHome();
}

$("#leave").addEventListener("click", leaveRoom);

$("#share").addEventListener("click", async () => {
  const url = `${location.origin}/?room=${roomId}`;
  const text = `Let's talk on Apni Awaaz. You'll hear me in your language. Code: ${roomId}`;
  try {
    if (navigator.share) {
      await navigator.share({ title: "Apni Awaaz", text, url });
      return;
    }
    await navigator.clipboard.writeText(url);
    toast("Invite link copied");
  } catch (error) {
    if (error?.name !== "AbortError") toast(`Share this code: ${roomId}`);
  }
});

function handleMessage(message) {
  switch (message.type) {
    case "joined":
      myPid = message.you;
      profile.mode = message.mode;
      store.set("aa_profile", profile);
      break;
    case "room":
      participants = message.participants;
      renderRoom();
      break;
    case "status":
      updateTurn(message.turn_id, { speaker: message.speaker, state: message.state, message: message.message });
      break;
    case "speaking":
      if (message.speaker !== myPid) {
        otherSpeaking = message.on ? message.speaker_name : null;
        renderRoom();
      }
      break;
    case "text":
      updateTurn(message.turn_id, message);
      break;
    case "audio_start":
      player.begin(message.sample_rate);
      showPlayingBadge(message.speaker_name);
      break;
    case "audio_end":
      clearTimeout(badgeTimer);
      badgeTimer = setTimeout(() => { $("#playing-badge").hidden = true; }, player.remaining * 1000 + 300);
      break;
    case "error":
      toast(message.message);
      if (message.fatal) leaveRoom();
      break;
  }
}

function showPlayingBadge(name) {
  clearTimeout(badgeTimer);
  const badge = $("#playing-badge");
  badge.textContent = `🔊 AI voice of ${name}`; // SRS NFR-4: always label cloned and AI speech
  badge.hidden = false;
}

function renderRoom() {
  const me = participants.find((p) => p.pid === myPid);
  const other = participants.find((p) => p.pid !== myPid);

  const people = participants.map((p) => {
    const li = document.createElement("li");
    const dot = document.createElement("span");
    dot.className = "dot";
    li.append(dot, `${p.pid === myPid ? "You" : p.name} · ${p.language_name}`);
    return li;
  });
  if (!other) {
    const li = document.createElement("li");
    li.className = "waiting";
    li.textContent = "Waiting for the other person…";
    people.push(li);
  }
  $("#people").replaceChildren(...people);

  const mode = me?.mode || profile.mode;
  for (const button of $$(".mode-toggle button")) {
    button.setAttribute("aria-checked", String(button.dataset.mode === mode));
  }
  $("#mode-hint").textContent = mode === "my_voice"
    ? "They hear your words translated, in your own cloned voice."
    : `They hear an expressive AI voice (${profile.voice}) that carries your emotion.`;

  const ready = Boolean(me && other && socket?.readyState === WebSocket.OPEN);
  $("#talk").disabled = !ready;
  if (!other) otherSpeaking = null;
  const status = $("#talk-status");
  status.classList.toggle("speaking", Boolean(otherSpeaking));
  if (!talking) {
    status.textContent = !me
      ? "Connecting…"
      : otherSpeaking
        ? `${otherSpeaking} is speaking…`
        : other
          ? `Speak ${languageName(profile.language)}. ${other.name} hears ${other.language_name}.`
          : "Tap Invite and send the link to the person you want to talk to.";
  }
}

for (const button of $$(".mode-toggle button")) {
  button.addEventListener("click", () => {
    const mode = button.dataset.mode;
    if (mode === "my_voice" && !hasVoice) {
      toast("Set up your voice first: Leave, then 'Set up my voice'.");
      return;
    }
    profile.mode = mode;
    store.set("aa_profile", profile);
    socket?.send(JSON.stringify({ type: "mode", mode }));
  });
}

// ---------- transcript ----------

const STATE_TEXT = {
  queued: "Waiting for the previous turn…",
  translating: "Translating…",
  done: "",
};

function updateTurn(turnId, data) {
  let turn = turns.get(turnId);
  if (!turn) {
    const li = document.createElement("li");
    li.className = `turn ${data.speaker === myPid ? "mine" : "theirs"}`;
    li.innerHTML = '<span class="who"></span><span class="main"></span><span class="sub"></span><span class="state"></span>';
    $("#transcript").append(li);
    turn = { li, mine: data.speaker === myPid };
    turns.set(turnId, turn);
  }
  Object.assign(turn, Object.fromEntries(Object.entries(data).filter(([, v]) => v !== undefined)));

  const { li, mine } = turn;
  const otherName = participants.find((p) => p.pid !== myPid)?.name || "They";
  const who = li.querySelector(".who");
  who.textContent = mine ? "You" : turn.speaker_name || otherName;
  if (turn.emotion && turn.mode === "expressive" && !/^(neutral|none|calm)$/i.test(turn.emotion)) {
    const chip = document.createElement("span");
    chip.className = "chip";
    chip.textContent = turn.emotion;
    who.append(chip);
  }
  if (turn.transcript !== undefined) {
    const translation = stripTags(turn.translation);
    // You read your own words; for the other person's turns you read the translation
    li.querySelector(".main").textContent = mine ? turn.transcript : translation;
    li.querySelector(".sub").textContent = mine
      ? `${otherName} heard: ${translation}`
      : `${languageName(turn.src)}: ${turn.transcript}`;
  }
  const state = li.querySelector(".state");
  state.classList.toggle("failed", turn.state === "failed");
  state.classList.toggle("working", turn.state === "queued" || turn.state === "translating");
  state.textContent = turn.state === "failed" ? turn.message || "Failed" : STATE_TEXT[turn.state] ?? "";
  li.scrollIntoView({ block: "end", behavior: "smooth" });
}

// ---------- push to talk ----------

const talkButton = $("#talk");

function startTalk(event) {
  if (talking || talkButton.disabled || !recorder || socket?.readyState !== WebSocket.OPEN) return;
  event?.preventDefault();
  if (event?.pointerId !== undefined) talkButton.setPointerCapture(event.pointerId);
  // No await here: a quick release during an await would leave the mic recording
  if (audioCtx.state === "suspended") audioCtx.resume();
  talking = true;
  // Stream while talking: Prisma transcribes as you speak, so the reply comes sooner
  socket.send(JSON.stringify({ type: "talk_start" }));
  const resampler = new StreamResampler(audioCtx.sampleRate);
  recorder.start((block) => {
    field?.setEnergy(rms(block) * 7); // the orb above the room moves with your voice
    const pcm = resampler.push(block);
    if (pcm.length && socket?.readyState === WebSocket.OPEN) socket.send(pcm.buffer);
  });
  talkButton.classList.add("recording");
  talkButton.querySelector(".talk-label").textContent = "Listening… let go to send";
  $("#talk-status").textContent = "";
  const timer = $("#talk-timer");
  talkTimer = setInterval(() => {
    const left = Math.ceil(config.max_turn_seconds - recorder.seconds);
    timer.classList.toggle("ending", left <= 10);
    timer.textContent = left <= 10 ? `${left}s left` : `${Math.floor(recorder.seconds)}s`;
    if (left <= 0) stopTalk(); // SRS FR-7: Prisma REST takes up to 60 s
  }, 200);
}

async function stopTalk() {
  if (!talking) return;
  talking = false;
  clearInterval(talkTimer);
  talkButton.classList.remove("recording");
  talkButton.querySelector(".talk-label").textContent = "Hold to talk";
  $("#talk-timer").textContent = "";
  const seconds = recorder.seconds;
  await recorder.stop(); // flushes the last block to the socket
  field?.setEnergy(0);
  if (socket?.readyState !== WebSocket.OPEN) {
    toast("Not connected. That turn wasn't sent.");
    return;
  }
  if (seconds < MIN_TURN_SECONDS) {
    socket.send(JSON.stringify({ type: "talk_cancel" }));
    toast("Hold the button down while you speak.");
    return;
  }
  socket.send(JSON.stringify({ type: "talk_end" }));
  renderRoom();
}

talkButton.addEventListener("pointerdown", startTalk);
for (const type of ["pointerup", "pointercancel", "lostpointercapture"]) {
  talkButton.addEventListener(type, stopTalk);
}
talkButton.addEventListener("contextmenu", (e) => e.preventDefault());

// Space bar works as push-to-talk on a laptop
function isSpaceTalk(e) {
  return e.code === "Space" && document.body.classList.contains("view-room") && !e.target.closest?.("input, select, textarea");
}
document.addEventListener("keydown", (e) => {
  if (!isSpaceTalk(e)) return;
  e.preventDefault(); // also stops Space from clicking a focused button
  if (!e.repeat) startTalk();
});
document.addEventListener("keyup", (e) => {
  if (!isSpaceTalk(e)) return;
  e.preventDefault();
  if (talking) stopTalk();
});

// ---------- entrance animations ----------

function settleAnimations() {
  const animated = [...$$(".appear"), $("#field")];
  for (const el of animated) {
    el.addEventListener("animationend", () => el.classList.add("is-in"), { once: true });
  }
  // If animations don't run at all (some low-power modes), show everything right away
  requestAnimationFrame(() => requestAnimationFrame(() => {
    const running = animated.some((el) => el.getAnimations?.().some((a) => a.playState === "running" || a.playState === "finished"));
    if (!running) for (const el of animated) el.classList.add("is-in");
  }));
}

// ---------- landing: scrolling ----------

function setUpLandingScroll() {
  // The floating header frosts over once the page moves
  const onScroll = () => document.body.classList.toggle("scrolled", scrollY > 24);
  addEventListener("scroll", onScroll, { passive: true });
  onScroll();

  if (!("IntersectionObserver" in window)) {
    document.body.classList.add("no-observer");
    return;
  }

  // Content rises in as it enters the screen
  const revealer = new IntersectionObserver((entries) => {
    for (const entry of entries) {
      if (!entry.isIntersecting) continue;
      entry.target.classList.add("is-visible");
      revealer.unobserve(entry.target);
    }
  }, { rootMargin: "0px 0px -8% 0px", threshold: 0.12 });
  for (const el of $$(".reveal")) revealer.observe(el);

  // A thin band across the middle of the screen decides which section is "current"
  const middle = { rootMargin: "-45% 0px -45% 0px" };

  // The particles take the shape of the current section
  const shapes = new IntersectionObserver((entries) => {
    if (!document.body.classList.contains("view-landing")) return;
    for (const entry of entries) {
      if (entry.isIntersecting) field?.setFormation(entry.target.dataset.formation);
    }
  }, middle);
  for (const section of $$("[data-formation]")) shapes.observe(section);

  // How it works: the phone shows whichever step is in the middle of the screen
  const phone = $(".phone");
  const steps = $$(".step");
  const setStep = (n) => {
    phone.dataset.step = n;
    for (const step of steps) step.classList.toggle("is-active", step.dataset.step === String(n));
  };
  const stepWatcher = new IntersectionObserver((entries) => {
    for (const entry of entries) if (entry.isIntersecting) setStep(entry.target.dataset.step);
  }, middle);
  for (const step of steps) stepWatcher.observe(step);

  // On phones the phone sits above the steps, so it plays through them on its own while visible
  const narrow = matchMedia("(max-width: 900px)");
  let cycle = null;
  new IntersectionObserver(([entry]) => {
    clearInterval(cycle);
    if (entry.isIntersecting && narrow.matches) {
      cycle = setInterval(() => setStep((Number(phone.dataset.step) % 3) + 1), 3200);
    }
  }, { threshold: 0.4 }).observe($(".phone-wrap"));
}

// ---------- landing: "Hear it yourself" samples ----------

const WAVE_BARS = 72;

function setUpSamples() {
  const cards = $$(".sample");
  // Clips load just before they scroll into view, so the hero stays light
  const loader = "IntersectionObserver" in window
    ? new IntersectionObserver((entries) => {
      for (const entry of entries) {
        if (!entry.isIntersecting) continue;
        loadSample(entry.target);
        loader.unobserve(entry.target);
      }
    }, { rootMargin: "400px 0px" })
    : null;
  for (const card of cards) {
    if (loader) loader.observe(card);
    else loadSample(card);
    card.querySelector(".play").addEventListener("click", () => toggleSample(card));
  }
  addEventListener("resize", () => {
    for (const card of cards) if (card.peaks) drawWave(card, card === currentSample?.card ? card.progress : 0);
  });
}

async function loadSample(card) {
  if (card.peaks || card.loading) return;
  card.loading = true;
  try {
    const data = await (await fetch(card.dataset.src)).arrayBuffer();
    const buffer = await new OfflineAudioContext(1, 1, 44100).decodeAudioData(data);
    const channel = buffer.getChannelData(0);
    const size = Math.floor(channel.length / WAVE_BARS);
    const peaks = [];
    for (let b = 0; b < WAVE_BARS; b++) {
      let peak = 0;
      for (let i = b * size; i < (b + 1) * size; i += 8) peak = Math.max(peak, Math.abs(channel[i]));
      peaks.push(peak);
    }
    const loudest = Math.max(...peaks) || 1;
    card.peaks = peaks.map((p) => p / loudest);
  } catch {
    card.peaks = Array.from({ length: WAVE_BARS }, (_, i) => 0.3 + 0.25 * Math.sin(i * 0.5));
  }
  drawWave(card, 0);
}

function drawWave(card, progress) {
  const canvas = card.querySelector(".wave");
  const dpr = Math.min(devicePixelRatio || 1, 2);
  const width = canvas.clientWidth;
  const height = canvas.clientHeight;
  if (!width) return;
  canvas.width = Math.round(width * dpr);
  canvas.height = Math.round(height * dpr);
  const g = canvas.getContext("2d");
  g.scale(dpr, dpr);
  const step = width / card.peaks.length;
  const barWidth = Math.max(1.5, step - 2);
  card.peaks.forEach((peak, i) => {
    const barHeight = Math.max(2, peak * height * 0.92);
    g.fillStyle = i / card.peaks.length < progress ? "#f26b1d" : "rgba(255, 255, 255, 0.32)";
    g.fillRect(i * step, (height - barHeight) / 2, barWidth, barHeight);
  });
}

function toggleSample(card) {
  if (currentSample?.card === card) {
    stopSample();
    return;
  }
  stopSample();
  const audio = new Audio(card.dataset.src);
  currentSample = { card, audio, frame: 0 };
  card.classList.add("playing");
  audio.addEventListener("ended", stopSample);
  audio.play().catch(() => {
    stopSample();
    toast("Couldn't play that clip.");
  });
  const tick = () => {
    if (currentSample?.audio !== audio) return;
    const progress = audio.duration ? audio.currentTime / audio.duration : 0;
    card.progress = progress;
    if (card.peaks) {
      drawWave(card, progress);
      // The particle waveform dances to the clip
      const peak = card.peaks[Math.min(card.peaks.length - 1, Math.floor(progress * card.peaks.length))];
      field?.pulse(peak * 0.9);
    }
    currentSample.frame = requestAnimationFrame(tick);
  };
  tick();
}

function stopSample() {
  if (!currentSample) return;
  const { card, audio, frame } = currentSample;
  currentSample = null;
  cancelAnimationFrame(frame);
  audio.pause();
  card.classList.remove("playing");
  card.progress = 0;
  if (card.peaks) drawWave(card, 0);
}

// ---------- start ----------

async function init() {
  try {
    field = createParticleField($("#field"));
  } catch (error) {
    console.warn("Particle background unavailable", error); // the page works without it
  }
  settleAnimations();
  setUpLandingScroll();
  setUpSamples();
  config = await (await fetch("/api/config")).json();
  if (!myVoice) {
    // A voice saved on the server by the first version of the app moves into this browser once
    try {
      const response = await fetch("/api/voice/migrate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ user_id: userId }),
      });
      const { voice } = await response.json();
      if (voice) setMyVoice(voice);
    } catch { /* nothing to move */ }
  }
  if (profile && !hasVoice) profile.mode = "expressive";
  if (profile && !config.languages[profile.language]) profile = null;
  goHome();
}

init();
