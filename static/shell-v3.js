/* Shortslab shell v3.
 *
 * One dense run page per mode, and one honest wait screen. Reached with ?ui=v3; the classic
 * chat shell stays the default and is untouched.
 *
 * Contract (the part that must never drift): buildRunForm() walks BOOT.manifest.run exactly as
 * static/chat-shell.js does - same always-fields, same on/"" hidden pairs, same text list, same
 * checkbox semantics - and posts to /run. Masters post to their MASTER_MANIFESTS action.
 *
 * State: one value store PER MODE (clip / ai). The classic shell kept one store and let the
 * Clip Short preset overwrite the AI Short settings on every render; here a mode's values are
 * only ever touched by that mode's page.
 */
"use strict";

const BOOT = JSON.parse(document.getElementById("v3-boot").textContent);
const T = BOOT.strings || {};
const OPT = BOOT.options || {};
const MAN = BOOT.manifest;
const REASONING = BOOT.reasoningConfig || {};
const REDUCED = !!(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches);
const DRAFT_TTL_MS = 24 * 3600 * 1000;
const DRAFT_KEY = "sl-v3-draft";
const CLOCK_OFFSET = (BOOT.now || Date.now() / 1000) - Date.now() / 1000;   // server - browser, seconds
const SEED_TTS_MODEL = "bytedance/seed-speech-tts-2.0";
const BOOTED_KEY = "sl-v3-booted";

/* Where the shell's own imagery lives. The preview harness rewrites "/static/" to a relative
   path inside the document only, so the script resolves against the stylesheet it was loaded
   with: same host, same prefix. */
const STATIC = (() => {
  // absolute on purpose: a relative url() inside a custom property resolves against the
  // stylesheet, not the document
  const link = document.querySelector('link[rel="stylesheet"][href*="shell-v3.css"]');
  const href = new URL(link ? link.getAttribute("href") : "/static/shell-v3.css", location.href).href;
  return href.slice(0, href.indexOf("shell-v3.css"));
})();

/* ------------------------------------------------------------------ the machines
   One setup per mode: the machine, what its screen shows, the three-line headline. The list is
   the real one (chat_ui.py UI_STRINGS + the v3 run pages); nothing here is invented.
     page: "v3"     -> this shell has a run page for it (clip / ai / enhance)
     page: <url>    -> the machine runs in the classic console; the zoom lands and hands over.
   The machine itself is the same for every mode (see MACHINE below): what a mode changes is what
   its screen says, what plays behind it, and the words on the left. */
const MODES = [
  { id: "clip", page: "v3", mode: "clip", name: "Clip Station", model: "CS-1",
    title: ["Clip", "Station"], discipline: "Footage editing", input: "Script", output: "Real clips, cut to your words",
    quiet: "Match TikTok and Instagram footage to your script. Edit, narrate and caption the sequence.",
    poster: "previews/clip_short_trash_poster.jpg", video: "previews/clip_short_trash_20s_hd.mp4" },
  { id: "ai", page: "v3", mode: "ai", name: "AI Video Station", model: "VS-2",
    title: ["AI Video", "Station"], discipline: "Generative video", input: "Script + direction", output: "Generated footage, shot by shot",
    quiet: "Generate images and video for each shot, then assemble them with narration and captions.",
    poster: "previews/band_ai_short_motion_poster.jpg", video: "previews/ai_short_pig_war_motion_20s_hd.mp4" },
  { id: "challenge", page: "v3", mode: "challenge", name: "Challenge Station", model: "CH-3",
    title: ["Challenge", "Station"], discipline: "Character-led stories", input: "Character sheet + direction", output: "Approved voice, 8 generated clips + finished edit",
    quiet: "Choose a culture-clash premise, then inspect every 10-second Seedance prompt before generation.",
    poster: "previews/challenge_reference_poster.jpg", video: "previews/challenge_reference.mp4" },
  { id: "longform", page: "v3", mode: "longform", name: "Sketch Station", model: "SX-3",
    title: ["Sketch", "Station"], discipline: "Illustrated explainers", input: "Narration", output: "Stickman explainer, 16:9 or 9:16",
    quiet: "Turn narration into a sequence of stickman illustrations, timed to the voiceover.",
    poster: "previews/band_sketch_longform_collage.jpg", video: "" },
  { id: "physics", page: "v3", mode: "physics", name: "Physics Bench", model: "PB-4",
    title: ["Physics", "Bench"], discipline: "Simulation", input: "Scene + parameters", output: "Blender simulation, solved",
    quiet: "Configure a Blender scene, vary its physical parameters and render the resulting motion.",
    poster: "previews/band_physics_sweep_poster.jpg", video: "previews/physics_sweep.mp4" },
  { id: "enhance", page: "v3", mode: "enhance", name: "Effects Station", model: "MD-6",
    title: ["Effects", "Station"], discipline: "Post-production", input: "Existing MP4", output: "Captions, sound and callouts",
    quiet: "Add captions, visual callouts or sound effects to an existing cut. Preview and export the result.",
    poster: "previews/band_enhance_video_poster.jpg", video: "previews/enhance_video_vfx_v2.mp4" },
];
/* programs this machine runs that have no place on the showroom line: reached by their route */
const EXTRA_MODES = [
  { id: "reddit", page: "v3", mode: "reddit", name: "Story Station", model: "RS-9" },
  { id: "library", page: "v3", mode: "library", name: "Library", model: "LB-0" },
];
const modeById = (id) => MODES.find(m => m.id === id) || EXTRA_MODES.find(m => m.id === id) || MODES[0];
const setupForMode = (mode) => MODES.find(m => m.mode === mode) || EXTRA_MODES.find(m => m.mode === mode) || MODES[0];

/* object-position of .photo in the stylesheet - the glass geometry has to agree with it */
const PHOTO_POS = [0.5, 0.62];

/* ------------------------------------------------------------------ small helpers */
const $ = (id) => document.getElementById(id);
const el = (tag, cls, html) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (html !== undefined) n.innerHTML = html;
  return n;
};
const esc = (s) => String(s == null ? "" : s)
  .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
async function jget(url) { const r = await fetch(url, { cache: "no-store" }); return r.json(); }
async function jpost(url, data) {
  const r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data || {}) });
  try { return await r.json(); } catch (e) { return { ok: r.ok }; }
}
function clock(sec) {
  sec = Math.max(0, Math.round(+sec || 0));
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  const mm = String(m).padStart(2, "0"), ss = String(s).padStart(2, "0");
  return h ? `${h}:${mm}:${ss}` : `${m}:${ss}`;
}
function roughMinutes(sec) {
  const m = Math.round((+sec || 0) / 60);
  if (m < 1) return "under a minute";
  if (m < 60) return `${m} min`;
  const h = Math.floor(m / 60), r = m % 60;
  return r ? `${h} h ${r} min` : `${h} h`;
}
function withUi(url) { return url + (url.includes("?") ? "&" : "?") + "ui=v3"; }
const hookWords = (s) => String(s == null ? "" : s).replace(/#/g, " ").replace(/\s+/g, " ").trim().toLowerCase();
function estimatedScriptTokens(text) {
  const chunks = String(text || "").trim().match(/[\p{L}\p{N}]+|[^\s\p{L}\p{N}]/gu) || [];
  return chunks.length;
}
function wordCount(text) { return (String(text || "").trim().match(/\S+/g) || []).length; }
function firstVal(options) { const d = (options || []).find(o => o.selected) || (options || [])[0]; return d ? d.value : ""; }
function labelFor(options, value) { const o = (options || []).find(x => x.value === value); return o ? o.label : (value || "-"); }
function nowSec() { return Date.now() / 1000 + CLOCK_OFFSET; }

/* TTS provider plumbing - identical to the classic shell so the same voices are offered. */
const isSeedTts = (model) => String(model || "") === SEED_TTS_MODEL ||
  ["seed-speech", "seed-speech-tts-2.0"].includes(String(model || ""));
const ttsProviderOf = (model) => (OPT.tts_provider || {})[String(model || "")] || (isSeedTts(model) ? "seed" : "gemini");
const ttsVoiceOptions = (model) => ({ seed: OPT.tts_voice_seed || [], inworld: OPT.tts_voice_inworld || [] }[ttsProviderOf(model)]
  || OPT.tts_voice_gemini || OPT.tts_voice || []);
// What to CALL the selected narrator. The dropdown has read its label since the server started
// writing one; the panel's corner and the bottom dock still printed the raw engine id beside it,
// so the same voice appeared twice on one screen under two different names.
const voiceLabel = (model, value) =>
  ((ttsVoiceOptions(model) || []).find(o => o.value === value) || {}).label || value || "";
function resetTtsVoice(v) {
  const choices = ttsVoiceOptions(v.tts_model);
  if (!choices.some(o => o.value === v.tts_voice))
    v.tts_voice = { seed: "stokie_en", inworld: "Dennis" }[ttsProviderOf(v.tts_model)] || (choices[0] || {}).value || "";
}
let _audio = null;
function previewTts(v) {
  _audio = _audio || new Audio();
  _audio.src = BOOT.voices_preview + encodeURIComponent(v.tts_voice || "") + "&model=" + encodeURIComponent(v.tts_model || "pro");
  _audio.play().catch(() => {});
}
function reasoningOptions(modelId, previous) {
  const cfg = REASONING[modelId];
  if (!cfg) return { options: [], value: "" };
  const valid = (cfg.options || []).some(o => o.value === previous);
  return { options: cfg.options || [], value: valid ? previous : cfg.defaultValue };
}

/* ------------------------------------------------------------------ state */
/* Region and search languages have no switch any more: Japan plus Japanese+English is what the
   measured search wants (native terms returned 476 post URLs against 72 for English alone), so the
   values are fixed in the store and still posted by buildRunForm. */
const REGION_LANGS = { japan: "ja,en", general: "en", switzerland: "de,en", history: "en" };
function baseDefaults() {
  // The SAME persisted defaults the classic shell and the legacy form start from (ui_state.json).
  const v = {};
  const st = BOOT.uiState || {};
  MAN.run.text.forEach(k => { v[k] = st[k] != null ? String(st[k]) : ""; });
  MAN.run.check.forEach(k => { v[k] = !!st[k]; });
  MAN.run.state_hidden.forEach(k => { v[k] = st[k] !== undefined ? !!st[k] : true; });
  v.background_music_enabled = !!st.background_music_enabled;
  if (!v.clip_source) v.clip_source = "generate";
  if (!v.scraping_engine) v.scraping_engine = "v4";
  if (!v.script_relevancy) v.script_relevancy = "90";
  if (!v.scrape_sort) v.scrape_sort = "ALL";
  if (!v.scrape_time_budget) v.scrape_time_budget = "3600";
  if (!v.scrape_platforms) v.scrape_platforms = "tiktok,x,instagram";
  if (!v.sfx_amount) v.sfx_amount = "medium";
  if (!v.vfx_amount) v.vfx_amount = "medium";
  if (st.add_visual_effects === undefined) v.add_visual_effects = true;
  if (st.out_captions === undefined) v.out_captions = true;
  if (st.out_sfx === undefined) v.out_sfx = true;
  if (st.out_transition_sfx === undefined) v.out_transition_sfx = true;
  if (st.halt_after_speech === undefined) v.halt_after_speech = false;
  if (!v.pipeline_version) v.pipeline_version = "v0.2";
  v.clip_short_format = "standard";
  v.script_token_limit = "";
  if (!v.speaker_name) v.speaker_name = "Narrator";
  if (st.motion_loop_seamless === undefined) v.motion_loop_seamless = false;
  v.motion_loop_unlimited = true;
  v.script = ""; v.gen_topic = ""; v.hook_text = ""; v.impact_word = "";
  // Custom search terms are per run, like the script - never carried over from ui_state.json.
  v.scrape_terms = "";
  v.loaded_project_mode = v.loaded_project_mode || "normal";
  v.loaded_project_source = "";
  let region = "";
  try { region = localStorage.getItem("sl-region") || ""; } catch (e) {}
  v.region = v.region || region || "japan";
  v.search_languages = v.search_languages || REGION_LANGS[v.region] || "en";
  // Captions are always the plain white word-by-word style - no styling controls, fixed payload.
  v.caption_active_style = "none"; v.caption_base_color = "#ffffff"; v.caption_active_color = "#ffffff";
  v.caption_stroke = "thin"; v.caption_uppercase_choice = "upper"; v.caption_size = v.caption_size || 84;
  return v;
}
/* The Clip Short preset, applied ONCE when the store is created and again as invariants at
   submit time - never on render, so nothing the user picked is undone by a repaint. */
function clipInvariants(v) {
  v.culture_facts_mode = true; v.visuals_from_script_mode = false; v.motion_loop_mode = false;
  v.clip_source = "scrape"; v.scraping_engine = "v4";
  v.enable_speaker_hook = false; v.speaker_image_path = "";
  v.out_web_images = false; v.out_wikimedia = false; v.out_gpt_images = false; v.out_video_clips = true;
  if (v.out_captions === undefined || v.out_captions === false) v.out_captions = true;
  v.out_sfx = false;
  if (v.out_transition_sfx === undefined) v.out_transition_sfx = true;
  if (!v.script_relevancy) v.script_relevancy = "90";
  v.scrape_sort = "ALL";
  v.scrape_platforms = "tiktok,instagram";
  if (v.v4_instagram_enabled === undefined || v.v4_instagram_enabled === "") v.v4_instagram_enabled = true;
  if (v.v4_tiktok_login_fallback === undefined || v.v4_tiktok_login_fallback === "") v.v4_tiktok_login_fallback = true;
  if (!v.v4_tiktok_discovery_provider) v.v4_tiktok_discovery_provider = "scrapedo";
  if (!v.v4_scrapedo_geo) v.v4_scrapedo_geo = "jp";
  if (!v.scrape_time_budget) v.scrape_time_budget = "3600";
  if (v.clip_short_format === "discovery") v.halt_after_speech = false;
  return v;
}
function modeDefaults(mode) {
  const v = baseDefaults();
  if (mode === "clip") {
    clipInvariants(v);
    v.tts_model = "pro"; v.tts_voice = "Laomedeia"; v.speaker_name = "Narrator";
    v.vision_model = "google/gemini-3.7-flash";
    v.clip_short_format = "standard"; v.script_token_limit = "";
    v.script_relevancy = "90"; v.out_sfx = false; v.out_transition_sfx = true;
  } else {
    v.culture_facts_mode = false; v.visuals_from_script_mode = true; v.motion_loop_mode = false;
    v.clip_source = "generate"; v.scraping_engine = "v4";
  }
  resetTtsVoice(v);
  return v;
}
/* stated before the store is built: freshState() reads them */
const SKETCH_DEFAULTS = {
  tts_model: "bytedance/seed-speech-tts-2.0", tts_voice: "jess_ja_es_id_pt_en_zh", tts_language: "en",
  tts_native_speed: "1.14", tts_volume: "1.05", reasoning_model: "google/gemini-3.7-flash",
};
// the same two constants the older console used for a 9:16 short; a different number here is
// how "how fast a short is read" silently drifted once before
const SHORTS_VOICE_SPEED = "1.14";
const SHORTS_VOICE_INSTRUCTION = "Upbeat, energetic and friendly. Bright, forward-leaning pace "
  + "with clear punchy emphasis on the key word of each line, a light smile in the voice, and "
  + "short confident pauses instead of drawn-out ones. Never shouty, never breathless.";
const PHYS_BRIEF_MODELS = [
  { id: "google/gemini-3.5-flash-lite", t: "Gemini 3.5 Flash Lite (fast)" },
  { id: "google/gemini-3.1-flash-lite", t: "Gemini 3.1 Flash Lite" },
  { id: "google/gemini-3.5-flash", t: "Gemini 3.5 Flash" },
  { id: "deepseek/deepseek-v4-flash-0731", t: "DeepSeek V4 Flash (cheapest)" },
  { id: "deepseek/deepseek-v4-pro", t: "DeepSeek V4 Pro" },
  { id: "anthropic/claude-opus-4.8", t: "Claude Opus 4.8 (most detailed)" },
];
const PHYS_QUALITY = [[12, "Draft", "grainy, quickest"], [24, "Standard", "what the finished shorts use"], [48, "Sharp", "clean, roughly twice the wait"]];
function freshState() {
  return {
    ui: "v3", view: "showroom", mode: "clip", slide: 0,
    clip: modeDefaults("clip"), ai: modeDefaults("ai"),
    enhance: { kind: "sfx", master: {}, fileName: "" },
    longform: longformDefaults(), physics: physicsDefaults(), viral: { topic: "" },
    challenge: { direction: "", ownTopic: "", topics: [], selected: -1, result: null, fileName: "" },
    reddit: { stories: [], story: null }, action: actionDefaults(), assets: { q: "", hidden: false },
    jobId: null, jobStatus: "", jobKind: "",
    projectSlug: "", projectTitle: "",
    savedAt: 0,
  };
}
let S = freshState();
let FILES = {};
const V = () => S[S.mode === "ai" ? "ai" : "clip"];
const isClip = () => S.mode === "clip";
const isDiscovery = () => isClip() && V().clip_short_format === "discovery";
const isOthers = () => isClip() && V().clip_short_format === "others_vs_king";

/* ------------------------------------------------------------------ draft persistence */
let saveTimer = null;
function persist() {
  clearTimeout(saveTimer);
  saveTimer = setTimeout(() => {
    S.savedAt = Date.now();
    const copy = JSON.parse(JSON.stringify(S));
    try { localStorage.setItem(DRAFT_KEY, JSON.stringify(copy)); } catch (e) {}
    jpost("/chat-state", copy).catch(() => {});
  }, 400);
}
function draftCandidates() {
  const out = [];
  const server = BOOT.chatState;
  if (server && server.ui === "v3" && server.savedAt) out.push(server);
  try {
    const raw = localStorage.getItem(DRAFT_KEY);
    if (raw) { const d = JSON.parse(raw); if (d && d.ui === "v3" && d.savedAt) out.push(d); }
  } catch (e) {}
  out.sort((a, b) => (b.savedAt || 0) - (a.savedAt || 0));
  return out;
}
function draftIsWorthRestoring(d) {
  if (!d || Date.now() - (d.savedAt || 0) > DRAFT_TTL_MS) return false;
  const vals = [d.clip, d.ai].filter(Boolean);
  return !!(d.jobId || vals.some(v => (v.script || "").trim() || (v.gen_topic || "").trim() || v.loaded_project_source)
    || (d.longform && (d.longform.script || "").trim()) || (d.viral && (d.viral.topic || "").trim())
    || (d.challenge && ((d.challenge.direction || "").trim() || (d.challenge.ownTopic || "").trim()
        || d.challenge.result))
    || (d.physics && (d.physics.scene || (d.physics.prompt || "").trim())));
}
function adoptDraft(d) {
  const base = freshState();
  S = { ...base, ...d, clip: { ...base.clip, ...(d.clip || {}) }, ai: { ...base.ai, ...(d.ai || {}) },
        enhance: { ...base.enhance, ...(d.enhance || {}) },
        longform: { ...base.longform, ...(d.longform || {}) }, physics: { ...base.physics, ...(d.physics || {}) },
        viral: { ...base.viral, ...(d.viral || {}) }, reddit: { ...base.reddit, ...(d.reddit || {}) },
        challenge: { ...base.challenge, ...(d.challenge || {}) },
        action: { ...base.action, ...(d.action || {}) }, assets: { ...base.assets, ...(d.assets || {}) } };
  S.ui = "v3";
  if (!["clip", "ai", "challenge", "enhance", "longform", "physics", "reddit"].includes(S.mode)) S.mode = "clip";
  if (!["showroom", "home", "wait", "assets"].includes(S.view)) S.view = "showroom";
  resetTtsVoice(S.clip); resetTtsVoice(S.ai);
}
function discardDraft() {
  try { localStorage.removeItem(DRAFT_KEY); } catch (e) {}
  jpost("/chat-state", { ui: "v3", savedAt: 0, discarded: true }).catch(() => {});
}

/* ------------------------------------------------------------------ controls */
function btn(label, fn, cls) {
  const b = el("button", "btn " + (cls || ""), label);
  b.type = "button";
  if (fn) b.addEventListener("click", fn);
  return b;
}
function svgIcon(name) {
  const paths = {
    play: '<path d="M8 5v14l11-7z" fill="currentColor" stroke="none"/>',
    check: '<path d="M20 6 9 17l-5-5"/>',
    x: '<path d="M18 6 6 18M6 6l12 12"/>',
    star: '<path d="m12 3 2.7 5.6 6.1.9-4.4 4.3 1 6.1L12 17l-5.4 2.9 1-6.1L3.2 9.5l6.1-.9z"/>',
    bolt: '<path d="M13 2 4 14h7l-1 8 9-12h-7z"/>',
    ext: '<path d="M14 4h6v6M20 4l-9 9M19 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V6a1 1 0 0 1 1-1h5"/>',
    down: '<path d="M12 4v12M6 11l6 6 6-6M4 20h16"/>',
  };
  return `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${paths[name] || ""}</svg>`;
}
function selectField(label, options, value, onChange, hint) {
  const f = el("div", "fld");
  if (label) f.appendChild(el("span", "lbl", esc(label)));
  const s = el("select"); s.setAttribute("aria-label", label || "Select option");
  (options || []).forEach(o => s.appendChild(new Option(o.label, o.value)));
  const have = [...s.options].some(o => o.value === value);
  s.value = have ? value : firstVal(options);
  if (!have && s.value !== value) onChange(s.value, true);
  s.addEventListener("change", () => { onChange(s.value, false); persist(); afterChange(); });
  f.appendChild(s);
  if (hint) f.appendChild(el("div", "hint", esc(hint)));
  return f;
}
function toggleField(label, value, onChange, hint) {
  const wrap = el("div", "fld");
  const l = el("label", "tgl" + (value ? "" : " off"));
  const i = el("input"); i.type = "checkbox"; i.checked = !!value;
  i.addEventListener("change", () => { l.classList.toggle("off", !i.checked); onChange(i.checked); persist(); afterChange(); });
  l.appendChild(el("span", "", esc(label)));
  l.appendChild(i);
  const track = el("span", "track"); track.setAttribute("aria-hidden", "true"); l.appendChild(track);
  wrap.appendChild(l);
  if (hint) wrap.appendChild(el("div", "hint", esc(hint)));
  return wrap;
}
function textField(label, value, onInput, placeholder, hint) {
  const f = el("div", "fld");
  if (label) f.appendChild(el("span", "lbl", esc(label)));
  const i = el("input"); i.type = "text"; i.autocomplete = "off"; i.value = value || ""; i.placeholder = placeholder || ""; i.setAttribute("aria-label", label || placeholder || "Text");
  i.addEventListener("input", () => { onInput(i.value); persist(); });
  i.addEventListener("change", afterChange);
  f.appendChild(i);
  if (hint) f.appendChild(el("div", "hint", esc(hint)));
  return f;
}
function rangeField(label, min, max, step, value, fmt, onInput, hint) {
  const f = el("div", "fld");
  f.appendChild(el("span", "lbl", esc(label)));
  const row = el("div", "range-row");
  const r = el("input"); r.type = "range"; r.min = min; r.max = max; r.step = step; r.value = value; r.setAttribute("aria-label", label);
  const val = el("span", "val", esc(fmt(value)));
  r.addEventListener("input", () => { val.textContent = fmt(r.value); onInput(r.value); persist(); });
  r.addEventListener("change", afterChange);
  row.appendChild(r); row.appendChild(val); f.appendChild(row);
  if (hint) f.appendChild(el("div", "hint", esc(hint)));
  return f;
}
function amountField(label, current, onChange) {
  const levels = ["low", "medium", "high"];
  return rangeField(label, 0, 2, 1, Math.max(0, levels.indexOf(current || "medium")), v => levels[+v],
    v => onChange(levels[+v]));
}
function segField(label, options, value, onChange) {
  const f = el("div", "fld");
  if (label) f.appendChild(el("span", "lbl", esc(label)));
  const seg = el("div", "seg");
  options.forEach(([val, lab]) => {
    const b = el("button", val === value ? "on" : "", esc(lab)); b.type = "button"; b.setAttribute("aria-pressed", String(val === value));
    b.addEventListener("click", () => { onChange(val); persist(); render(); });
    seg.appendChild(b);
  });
  f.appendChild(seg);
  return f;
}
function group(title, summary, ...nodes) {
  const g = el("section", "grp");
  const head = el("div", "grp-head");
  head.appendChild(el("span", "label", esc(title)));
  head.appendChild(el("span", "sum", esc(summary || "")));
  g.appendChild(head);
  const body = el("div", "grp-body");
  nodes.filter(Boolean).forEach(n => body.appendChild(n));
  g.appendChild(body);
  return g;
}
let toastTimer = null;
function toast(text, isErr) {
  document.querySelectorAll(".toast").forEach(t => t.remove());
  const t = el("div", "toast" + (isErr ? " err" : ""), esc(text));
  t.setAttribute("role", "status");
  document.body.appendChild(t);
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.remove(), isErr ? 6000 : 3500);
}
function modal(title, bodyNode) {
  const previous = document.activeElement;
  const scrim = el("div", "scrim");
  const m = el("div", "modal");
  m.setAttribute("role", "dialog"); m.setAttribute("aria-modal", "true"); m.setAttribute("aria-label", title); m.tabIndex = -1;
  const close = () => { scrim.remove(); document.removeEventListener("keydown", onKey); if (previous && previous.isConnected) previous.focus(); };
  const head = el("div", "modal-head");
  head.appendChild(el("span", "h2", esc(title)));
  head.appendChild(btn("Close", close, "sm ghost"));
  m.appendChild(head); m.appendChild(bodyNode); scrim.appendChild(m);
  scrim.addEventListener("click", e => { if (e.target === scrim) close(); });
  const onKey = (e) => {
    if (!scrim.isConnected) { document.removeEventListener("keydown", onKey); return; }
    if (e.key === "Escape") { e.preventDefault(); close(); return; }
    if (e.key !== "Tab") return;
    const items = [...m.querySelectorAll('button:not([disabled]), a[href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex="0"]')].filter(n => n.getClientRects().length);
    const first = items[0], last = items[items.length - 1];
    if (!first) { e.preventDefault(); m.focus(); }
    else if (e.shiftKey && (document.activeElement === first || document.activeElement === m)) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
  };
  document.addEventListener("keydown", onKey);
  $("v3-layer").appendChild(scrim);
  m.focus();
  return scrim;
}

/* Called after any control commits a value: the dock summary and the group captions re-read
   state, and whichever summary segment changed gets the "committed" flash. */
function afterChange() { renderDock(); renderGroupSums(); }

/* ------------------------------------------------------------------ top bar */
const STRIPES = '<svg viewBox="0 0 30 20" aria-hidden="true"><rect y="0" width="30" height="3.4" rx="1.7" fill="#e0524a"/><rect y="4.15" width="26" height="3.4" rx="1.7" fill="#f0a63a"/><rect y="8.3" width="30" height="3.4" rx="1.7" fill="#e7d54a"/><rect y="12.45" width="24" height="3.4" rx="1.7" fill="#5cb96e"/><rect y="16.6" width="30" height="3.4" rx="1.7" fill="#4c6fe0"/></svg>';
function renderTop() {
  const top = $("v3-top"); top.innerHTML = "";
  const inScreen = S.view === "home" || S.view === "wait" || S.view === "assets";
  const onStart = S.view === "start";
  const brand = el("a", "brand", STRIPES + "<span>SHORTSLAB</span>"); brand.href = withUi("/");
  brand.title = "Shortslab Studio";
  brand.addEventListener("click", e => { e.preventDefault(); goShowroom(true); });
  top.appendChild(brand);
  if (inScreen) {
    // the program's title bar: which machine, which program
    const masterKinds = ["sfx", "visual", "caption", "asmr", "actionedit"];
    const setup = S.view === "assets" ? modeById("library")
      : (S.view === "wait" && masterKinds.includes(S.jobKind)) ? modeById("enhance") : setupForMode(S.mode);
    const nm = el("span", "machine-name");
    // no model number in front of the name - the owner asked for "CS-1"/"VS-2" to be gone,
    // and they came back with the studio rewrite.
    nm.innerHTML = `<b>${esc(setup.name.toUpperCase())}</b>` +
      (S.view === "wait" ? '<small>· at work</small>' : "");
    top.appendChild(nm);
  } else if (onStart) {
    // the five names are the cards below; a tab row here was the same five words twice
  } else {
    const tabs = el("div", "tabs"); tabs.setAttribute("role", "navigation"); tabs.setAttribute("aria-label", "Creative tools");
    MODES.forEach((m, i) => {
      const t = el("button", "tab", esc(m.name)); t.type = "button"; t.setAttribute("aria-label", "Open " + m.name);
      t.classList.toggle("studio-nav-active", S.slide === i);
      t.addEventListener("click", () => goSlide(i));
      tabs.appendChild(t);
    });
    top.appendChild(tabs);
  }
  top.appendChild(el("span", "top-spacer"));
  if (S.jobId && ["running", "cancelling", "awaiting_approval"].includes(S.jobStatus) && S.view !== "wait") {
    const pill = el("a", "runpill" + (S.jobStatus === "awaiting_approval" ? " paused" : ""));
    pill.href = withUi("/job?id=" + encodeURIComponent(S.jobId));
    pill.innerHTML = '<i aria-hidden="true"></i><span id="pill-clock">' + (S.jobStatus === "awaiting_approval" ? "needs you" : "running") + "</span>";
    pill.addEventListener("click", e => { e.preventDefault(); openJob(S.jobId); });
    top.appendChild(pill);
  } else if (OTHER_RUNNING.length && S.view !== "wait") {
    const pill = el("a", "runpill");
    pill.href = withUi("/job?id=" + encodeURIComponent(OTHER_RUNNING[0].id));
    pill.innerHTML = `<i aria-hidden="true"></i><span>${OTHER_RUNNING.length} running</span>`;
    pill.addEventListener("click", e => { e.preventDefault(); openJob(OTHER_RUNNING[0].id); });
    top.appendChild(pill);
  }
  const quick = btn('<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" aria-hidden="true"><circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 4 4"/></svg><span>Find a project</span><kbd>Ctrl K</kbd>', openProjectSearch, "studio-search-btn");
  quick.setAttribute("aria-label", "Find a project"); quick.setAttribute("aria-keyshortcuts", "Control+k Meta+k");
  top.appendChild(quick);
  const links = el("div", "top-links");
  if (S.view !== "assets") {
    const lib = el("a", "top-link btn sm", "Library"); lib.href = "/assets"; lib.title = "Every project this machine has made";
    lib.addEventListener("click", e => { if (e.ctrlKey || e.metaKey || e.shiftKey || e.button) return; e.preventDefault(); goAssets(); });
    links.appendChild(lib);
  }
  // No link out to the old interface. The owner's rule is that no click inside the app may end
  // up in the chat shell; it is still reachable by typing ?ui=chat, which is where the Sketch
  // Station's frame editor still lives.
  if (inScreen) {
    const back = el("a", "top-link back", "Studio"); back.href = withUi("/");
    back.title = "Back to the studio (Esc)";
    back.addEventListener("click", e => { e.preventDefault(); goShowroom(); });
    links.appendChild(back);
  }
  top.appendChild(links);
}
let OTHER_RUNNING = [];
async function refreshJobs() {
  try {
    const d = await jget("/jobs-list");
    OTHER_RUNNING = (d.jobs || []).filter(j => ["running", "awaiting_approval"].includes(j.status) && j.id !== S.jobId);
    if (S.view !== "wait") renderTop();
    const deck = $("start-deck");
    if (deck && deck.dataset.jobId !== String(OTHER_RUNNING[0]?.id || "")) paintDeck(deck);
  } catch (e) {}
}

/* ------------------------------------------------------------------ home: the run page */
let pendingDraft = null;
function goHome() {
  // the run page of the current machine - "home" is where a mode's settings live
  stopPolling();
  S.view = "home";
  history.replaceState(null, "", ["clip", "ai"].includes(S.mode) ? withUi("/") : routeFor(S.mode));
  render();
}
function goShowroom(closing) {
  if (!SHOWROOM_3D) { stopPolling(); S.view = "start"; render(); return; }
  stopPolling();
  const land = () => {
    S.view = "showroom";
    history.replaceState(null, "", withUi("/"));
    persist(); render(); $("v3-main").scrollTop = 0;
  };
  // Leaving a station by the wordmark plays the last beats of the boot sequence: the machine
  // switching off, without the wordmark and copyright a real boot would show (flash).
  if (closing && S.view !== "showroom") { splash(land, { force: true, flash: 130 }); return; }
  land();
}

let disposeStartMotion = null;
function render() {
  if (disposeStartMotion) { disposeStartMotion(); disposeStartMotion = null; }
  if (blenderScene) { blenderScene.dispose(); blenderScene = null; }
  const b = document.body;
  b.classList.toggle("is-wait", S.view === "wait");
  b.classList.toggle("is-showroom", S.view === "showroom");
  b.classList.toggle("is-start", S.view === "start");
  b.classList.toggle("is-screen",
    S.view === "home" || S.view === "wait" || S.view === "assets" || S.view === "start");
  $("v3").dataset.view = S.view;
  renderTop();
  const main = $("v3-main"); main.innerHTML = "";
  const dock = $("v3-dock"); dock.hidden = true; dock.innerHTML = "";
  if (S.view === "wait") { setBackdrop(null); renderWait(main); return; }
  setTitle("Shortslab"); setFavicon(null);
  if (S.view === "showroom") { renderShowroom(main); return; }
  setBackdrop(null);
  if (S.view === "start") { renderStartScreen(main); return; }
  if (S.view === "assets") { setTitle("Library — Shortslab"); renderAssets(main); }
  else if (S.mode === "challenge") renderChallenge(main);
  else if (S.mode === "enhance") renderEnhance(main);
  else if (S.mode === "longform") renderLongform(main);
  else if (S.mode === "physics") renderPhysics(main);
  else if (S.mode === "reddit") renderReddit(main);
  else renderRunPage(main);
  main.firstElementChild && main.firstElementChild.classList.add("view-enter");
}
function restoreBanner() {
  const d = pendingDraft;
  const when = new Date(d.savedAt);
  const vals = d[d.mode === "ai" ? "ai" : "clip"] || {};
  const first = (vals.script || vals.gen_topic || "").split(/\n/)[0].slice(0, 80);
  const b = el("div", "restore");
  b.setAttribute("role", "region"); b.setAttribute("aria-label", "Unfinished setup");
  b.appendChild(el("b", "", "Unfinished setup"));
  b.appendChild(el("span", "cap", esc(
    // STATION_OF is the map the rest of the app uses. This one covered three modes and then
    // printed the raw route slug - "physics", "longform", "reddit" - and called
    // the two it did cover "Clip Short" and "AI Short" while every other screen says Station.
    `${when.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })} · ${STATION_OF[d.mode] || STATION_OF[""] }`
    + (d.jobId ? " · a run was open" : "") + (first ? ` · "${first}${first.length >= 80 ? "…" : ""}"` : ""))));
  const acts = el("div", "actions");
  // THE ONLY FILLED BUTTON ON THE PAGE WAS THIS ONE, AND IT LEFT THE PAGE. A user who clicked
  // "Sketch" on the home grid arrived at Sketch Station with exactly one saturated control on
  // screen - and it reopened Physics Bench, because the unfinished draft was a Physics one. The
  // station's own objective sat disabled and grey at the bottom of the window. The fill belongs
  // to the thing this screen is for; a way back to other work is a quiet offer, and it says
  // which work, so it cannot be mistaken for this station's next step.
  const home = STATION_OF[d.mode] || STATION_OF[""];
  acts.appendChild(btn(home === (STATION_OF[S.mode] || STATION_OF[""]) ? "Resume" : "Resume " + home,
                       resumeDraft, "sm"));
  acts.appendChild(btn("Start fresh", () => { pendingDraft = null; discardDraft(); render(); }, "ghost sm"));
  b.appendChild(acts);
  return b;
}
function resumeDraft() {
  const d = pendingDraft; if (!d) return;
  adoptDraft(d); pendingDraft = null;
  if (S.jobId && ["running", "cancelling", "awaiting_approval"].includes(S.jobStatus)) { openJob(S.jobId); return; }
  S.view = "home"; render();
}
/* the same unfinished setup, on the stage: a note on cream paper with a dotted edge */
function couponNote() {
  const d = pendingDraft;
  const when = new Date(d.savedAt);
  const vals = d[d.mode === "ai" ? "ai" : "clip"] || {};
  const first = (vals.script || vals.gen_topic || "").split(/\n/)[0].slice(0, 60);
  const setup = setupForMode(d.mode);
  const c = el("aside", "coupon");
  c.setAttribute("aria-label", "Unfinished setup");
  c.innerHTML = '<svg class="scissors" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true"><circle cx="6" cy="6" r="3"/><circle cx="6" cy="18" r="3"/><path d="M20 4 8.1 15.9M14.5 11.5 20 20"/></svg>';
  c.appendChild(el("b", "", "Left on the " + esc(setup.name)));
  c.appendChild(el("span", "cap", esc(when.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }))
    + (d.jobId ? " · a run was open" : "") + (first ? ` · “${esc(first)}${first.length >= 60 ? "…" : ""}”` : "")));
  const acts = el("div", "actions");
  acts.appendChild(btn("Sit back down", () => {
    const d2 = pendingDraft; adoptDraft(d2); pendingDraft = null;
    if (S.jobId && ["running", "cancelling", "awaiting_approval"].includes(S.jobStatus)) { openJob(S.jobId); return; }
    const i = MODES.findIndex(m => m.mode === S.mode); if (i >= 0) S.slide = i;
    enterMachine(setupForMode(S.mode));
  }, "sm"));
  acts.appendChild(btn("Start fresh", () => { pendingDraft = null; discardDraft(); render(); }, "ghost sm"));
  c.appendChild(acts);
  return c;
}
function renderRunPage(main) {
  const v = V();
  const page = el("div", "page");
  if (pendingDraft) page.appendChild(restoreBanner());
  const head = el("div", "page-head");
  head.appendChild(el("h1", "h1", isClip() ? "Clip Station" : "AI Video Station"));
  head.appendChild(el("span", "muted", isClip()
    ? "Real footage from TikTok and Instagram, matched to your script."
    : "Generated video and images, cut to your script."));
  head.appendChild(el("span", "cap", "Script → Footage → Edit → Export"));
  page.appendChild(head);
  const grid = el("div", "run-grid");
  grid.appendChild(scriptPanel(v));
  const groups = el("div", "groups"); groups.id = "groups";
  groups.appendChild(footageGroup(v));
  groups.appendChild(directorGroup(v));
  groups.appendChild(layersGroup(v));
  groups.appendChild(runGroup(v));
  grid.appendChild(groups);
  page.appendChild(grid);
  // no recent-projects strip under the settings: the Library is one click away in the top bar,
  // and the run page is for the run you are about to start
  main.appendChild(page);
  renderDock();
}

/* ---- script panel */
function scriptPanel(v) {
  const p = el("section", "panel"); p.id = "script-panel";
  const head = el("div", "panel-head");
  head.appendChild(el("span", "label", "Script"));
  if (isClip()) {
    const seg = el("div", "seg"); seg.setAttribute("aria-label", "Clip Short format");
    [["standard", "Fact Short"], ["discovery", "Discovery"], ["others_vs_king", "Others doing X"]].forEach(([val, lab]) => {
      const b = el("button", v.clip_short_format === val ? "on" : "", esc(lab)); b.type = "button"; b.setAttribute("aria-pressed", String(v.clip_short_format === val));
      b.addEventListener("click", () => { v.clip_short_format = val; v.script_token_limit = ""; clipInvariants(v); persist(); render(); });
      seg.appendChild(b);
    });
    head.appendChild(seg);
  }
  const meter = el("span", "cap"); meter.id = "script-meter";
  head.appendChild(meter);
  p.appendChild(head);

  if (isOthers()) {
    p.appendChild(el("div", "hint", "Name one filmable action. The editor finds several recognizable attempts, then reserves the most astonishing matching clip for the final payoff."));
    p.appendChild(textField("Action", v.others_action, x => v.others_action = x,
      "e.g. high jumping, skiing, diving, skateboarding", "Leave it blank and the agent chooses a fresh, highly visual action."));
  } else if (isDiscovery()) {
    p.appendChild(el("div", "hint", "Discovery gathers real footage first and writes the fact script around it. Add *instructions* after the topic to steer it, e.g. *use funny scenes*."));
    p.appendChild(textField("Topic", v.gen_topic, x => v.gen_topic = x, "e.g. Japanese vending machines *use funny scenes*"));
    p.appendChild(toggleField("Keep some of the original audio", !!v.discovery_keep_original_audio, x => v.discovery_keep_original_audio = x,
      "Alternates voiceover with selected source moments; the narrator mutes while the original plays."));
    const lib = el("div", "fld row");
    lib.appendChild(el("span", "lbl", v.candidate_url ? "Using a saved candidate" : "Or pin one saved candidate"));
    const acts = el("div", "chips");
    if (v.candidate_url) acts.appendChild(btn("Unpin", () => { v.candidate_url = ""; persist(); render(); }, "sm ghost"));
    acts.appendChild(btn("Candidate library", openCandidateLibrary, "sm"));
    lib.appendChild(acts); p.appendChild(lib);
  } else {
    const ta = el("textarea", "script"); ta.id = "script-box"; ta.setAttribute("aria-label", "Video script");
    ta.placeholder = T.script_placeholder || "Paste or type your script...";
    ta.value = v.script || "";
    ta.addEventListener("input", () => { v.script = ta.value; paintMeter(); persist(); });
    ta.addEventListener("change", afterChange);
    ta.addEventListener("keydown", e => { if ((e.ctrlKey || e.metaKey) && e.key === "Enter") { e.preventDefault(); submitRun(); } });
    p.appendChild(ta);
  }
  function paintMeter() {
    const m = meter;
    if (isOthers() || isDiscovery()) { m.textContent = isDiscovery() ? "The agent writes the script" : "No script - the action is enough"; return; }
    const words = wordCount(v.script), tokens = estimatedScriptTokens(v.script);
    const target = isClip() ? " · aim for 100-140 words" : "";
    m.textContent = `${words} words · ~${tokens} tokens${target}`;
    m.classList.toggle("over", isClip() && tokens > 130);
  }
  paintMeter();

  // narrator - on the script panel, where the words are
  if (!isOthers()) {
    const n = el("div", "narr");
    n.appendChild(el("span", "lbl", "Narrator"));
    const vsel = el("select");
    resetTtsVoice(v);
    ttsVoiceOptions(v.tts_model).forEach(o => vsel.appendChild(new Option(o.label, o.value)));
    vsel.value = v.tts_voice; vsel.setAttribute("aria-label", "Voice");
    vsel.addEventListener("change", () => { v.tts_voice = vsel.value; persist(); afterChange(); });
    n.appendChild(vsel);
    n.appendChild(btn(svgIcon("play") + " Preview", () => previewTts(v), "sm"));
    n.appendChild(el("span", "lbl", "Engine"));
  const msel = el("select"); msel.setAttribute("aria-label", "TTS model");
    (OPT.tts_model || []).forEach(o => msel.appendChild(new Option(o.label, o.value)));
    if ([...msel.options].some(o => o.value === v.tts_model)) msel.value = v.tts_model; else { v.tts_model = msel.value; resetTtsVoice(v); }
    msel.addEventListener("change", () => { v.tts_model = msel.value; resetTtsVoice(v); persist(); render(); });
    n.appendChild(msel);
    p.appendChild(n);
    if (ttsProviderOf(v.tts_model) === "seed") p.appendChild(seedSettings(v));
    if (!isClip()) {
      p.appendChild(toggleField(T.speaker_video || "Speaker video (talking-head hook)", v.enable_speaker_hook, x => { v.enable_speaker_hook = x; render(); }));
      if (v.enable_speaker_hook) p.appendChild(speakerGallery(v));
    }
  }
  if (!isDiscovery() && !isOthers()) p.appendChild(generatorBlock(v));
  return p;
}
function seedSettings(v) {
  const box = el("div", "fld-grid"); box.style.marginTop = "10px";
  box.appendChild(textField("Delivery direction", v.tts_voice_instruction, x => v.tts_voice_instruction = x, "e.g. warm, intimate, energetic"));
  box.appendChild(selectField("Language", OPT.seed_tts_languages || [], v.tts_language || "", x => v.tts_language = x));
  const num = (label, key, min, max, step, fallback) => {
    const f = el("div", "fld"); f.appendChild(el("span", "lbl", esc(label)));
    const i = el("input"); i.type = "number"; i.min = min; i.max = max; i.step = step;
    // The label is a sibling <span>, which a screen reader does not connect to the field: these
    // three read out as "edit text, blank". Name them.
    i.setAttribute("aria-label", label);
    i.value = v[key] == null || v[key] === "" ? fallback : v[key];
    i.addEventListener("input", () => { v[key] = i.value; persist(); });
    f.appendChild(i); return f;
  };
  // WHAT A NARRATION NEEDS, AND WHAT AN ENCODER NEEDS. Delivery and language are choices
  // about the performance; a sample rate, a codec and three unitless floats are the
  // encoder's paperwork, and they sat in the same grid at the same weight as "Narrator".
  // Someone making a stickman explainer should not have to choose between 8000 Hz and
  // 48000 Hz to get started.
  const adv = el("details", "adv");
  adv.appendChild(el("summary", "", "Audio settings"));
  const deep = el("div", "fld-grid");
  adv.appendChild(deep);
  deep.appendChild(num("Speed", "tts_native_speed", "0.5", "2", "0.1", "1"));
  deep.appendChild(num("Volume", "tts_volume", "0.5", "2", "0.1", "1"));
  deep.appendChild(num("Pitch", "tts_pitch", "-12", "12", "1", "0"));
  deep.appendChild(selectField("Sample rate", [8000, 16000, 22050, 24000, 32000, 44100, 48000].map(x => ({ value: String(x), label: x + " Hz" })),
    v.tts_sample_rate || "24000", x => v.tts_sample_rate = x));
  deep.appendChild(selectField("Output", [{ value: "mp3", label: "MP3" }, { value: "opus", label: "Opus" }], v.tts_output_format || "mp3", x => v.tts_output_format = x));
  const wrap = el("div", "");
  wrap.style.marginTop = "10px";
  box.style.marginTop = "0";
  wrap.appendChild(box); wrap.appendChild(adv);
  return wrap;
}
function speakerGallery(v) {
  const wrap = el("div", "fld");
  wrap.appendChild(el("span", "lbl", T.speaker_image || "Speaker image"));
  const grid = el("div", "chips"); wrap.appendChild(grid);
  fetch("/?legacy_ui=1").then(r => r.text()).then(html => {
    const re = /data-path="([^"]+)"[^>]*>\s*<img[^>]*src="([^"]+)"/g;
    let m, n = 0;
    while ((m = re.exec(html)) && n < 40) {
      n++;
      const b = el("button", "btn sm" + (v.speaker_image_path === m[1] ? " on" : ""));
      b.type = "button"; b.innerHTML = `<img src="${esc(m[2])}" alt="" style="width:28px;height:28px;border-radius:4px;object-fit:cover">`;
      const path = m[1];
      b.addEventListener("click", () => { v.speaker_image_path = path; FILES.speaker_image_file = null; persist(); render(); });
      grid.appendChild(b);
    }
    if (!n) grid.appendChild(el("span", "cap", "No speaker images yet - upload one."));
  }).catch(() => {});
  const up = btn("Upload image", () => pickFile("image/*", f => { FILES.speaker_image_file = f; v.speaker_image_path = ""; toast("Speaker image: " + f.name); afterChange(); }), "sm");
  up.style.marginTop = "6px"; wrap.appendChild(up);
  return wrap;
}
function generatorBlock(v) {
  const g = el("div", "gen");
  g.appendChild(el("span", "label", "Script creator"));
  const topic = el("input"); topic.type = "text"; topic.placeholder = "Topic (optional - empty lets it pick a viral one)";
  topic.setAttribute("aria-label", "Script topic"); topic.value = v.gen_topic || ""; topic.addEventListener("input", () => { v.gen_topic = topic.value; persist(); });
  g.appendChild(topic);
  const acts = el("div", "actions");
  const gb = btn("Generate script", async () => {
    gb.disabled = true; gb.textContent = "Writing…";
    try {
      const r = await fetch("/generate-script", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ topic: topic.value.trim(), instructions: dir.value.trim(), format_mode: "standard", token_limit: null,
          reasoning_model: v.reasoning_model || "google/gemini-3.7-flash", clip_source: v.clip_source || "scrape" }) });
      const d = await r.json();
      if (!d.ok) throw new Error(d.error || "no script");
      v.script = d.script; v.hook_keywords = JSON.stringify(d.hook_keywords || []); v.hook_text = ""; v.impact_word = "";
      persist(); render(); toast("Script generated - review it in the box.");
    } catch (e) { toast("Script generation failed: " + e, true); }
    gb.disabled = false; gb.textContent = "Generate script";
  }, "sm");
  acts.appendChild(gb);
  acts.appendChild(btn("Recent", async () => {
    const old = g.querySelector(".recent-scripts"); if (old) { old.remove(); return; }
    let d = { generated: [], used: [] };
    try { d = await jget("/recent-scripts"); } catch (e) {}
    const box = el("div", "recent-scripts");
    const add = (items, isGen) => (items || []).forEach(it => {
      const b = el("button"); b.type = "button";
      b.innerHTML = `<b>${esc(isGen ? (it.topic || "Generated") : it.project)}</b><span>${esc(it.preview)}</span>`;
      b.addEventListener("click", () => { v.script = it.script; v.hook_text = ""; v.impact_word = ""; persist(); render(); });
      box.appendChild(b);
    });
    add(d.generated, true); add(d.used, false);
    if (!box.children.length) box.appendChild(el("span", "cap", "No scripts yet."));
    box.style.gridColumn = "1 / -1";
    g.appendChild(box);
  }, "sm ghost"));
  g.appendChild(acts);
  const dir = el("textarea"); dir.placeholder = "Direction (optional): tone, structure, angle";
  dir.setAttribute("aria-label", "Script direction"); dir.value = v.script_generator_instructions || "";
  dir.addEventListener("input", () => { v.script_generator_instructions = dir.value; persist(); });
  g.appendChild(dir);
  return g;
}
async function openCandidateLibrary() {
  const v = V();
  let d = null; try { d = await jget("/candidate-library"); } catch (e) {}
  const body = el("div"); body.style.cssText = "padding:14px 16px; overflow:auto;";
  const rows = (d && d.candidates) || [];
  if (!rows.length) body.appendChild(el("p", "muted", "No saved candidates yet - every discovery run stores the candidates it shows here."));
  const grid = el("div", "cands");
  rows.forEach(r => {
    const c = el("div", "cand");
    c.appendChild(el("b", "", esc(r.title || r.desc || "Candidate")));
    c.appendChild(el("span", "cap", esc(`@${r.author || ""} · ${r.dur}s · ${(+r.likes || 0).toLocaleString()} likes` + (r.appeal ? ` · appeal ${r.appeal}/10` : "") + (r.picked ? " · used" : ""))));
    if (r.video_url) { const vp = el("video"); vp.src = r.video_url; vp.controls = true; vp.preload = "metadata"; c.appendChild(vp); }
    else if (r.sheet_url) { const im = el("img"); im.src = r.sheet_url; im.loading = "lazy"; im.alt = ""; c.appendChild(im); }
    c.appendChild(btn("Use this candidate", () => { v.candidate_url = r.url || ""; persist(); m.remove(); render(); }, "sm primary"));
    grid.appendChild(c);
  });
  body.appendChild(grid);
  const m = modal("Candidate library", body);
}

/* ---- right column groups */
function footageSummary(v) {
  if (!isClip()) return `${labelFor(OPT.video_model, v.video_model)} · ${labelFor(OPT.image_model, v.image_model)}`;
  const prov = v.v4_tiktok_discovery_provider === "brightdata" ? "Bright Data" : "Scrape.do";
  const geo = { jp: "Japan", sg: "Asia", us: "US" }[v.v4_scrapedo_geo] || v.v4_scrapedo_geo || "";
  return `${prov}${v.v4_tiktok_discovery_provider !== "brightdata" && geo ? " · " + geo : ""} · ${Math.round(+v.scrape_time_budget / 60)} min`;
}
function footageGroup(v) {
  const nodes = [];
  if (isClip()) {
    nodes.push(el("div", "hint", "V4: rendered TikTok discovery, then visual review. Boxed captions, letterboxing, stalls and repeated sources are rejected before anything is assigned."));
    if (!isDiscovery()) {
      // Discovery is always Scrape.do rendered search - Bright Data cannot deliver TikTok media
      // (measured: 210 vs 54 post URLs, 4/9 vs 1/9 beats covered), so there is nothing to choose.
      v.v4_tiktok_discovery_provider = "scrapedo";
      // The region row is gone by request; the payload still carries the value the runs use.
      v.v4_scrapedo_geo = v.v4_scrapedo_geo || "jp";
      nodes.push(rangeField("Script relevancy", 0, 100, 5, v.script_relevancy || "90", x => x + "%", x => v.script_relevancy = x));
    } else {
      nodes.push(el("div", "hint", "Discovery searches for one coherent native 9:16 source and opens on its strongest on-topic moment."));
    }
    if (!isDiscovery()) {
      nodes.push(el("div", "subcap", "Data source"));
      nodes.push(toggleField("Use TikTok login only when post delivery fails", !!v.v4_tiktok_login_fallback, x => v.v4_tiktok_login_fallback = x));
      nodes.push(toggleField("Include Bright Instagram Reels", !!v.v4_instagram_enabled, x => v.v4_instagram_enabled = x));
    }
    nodes.push(rangeField("Search time", 1800, 7200, 300, v.scrape_time_budget || "3600", x => Math.round(+x / 60) + " min",
      x => v.scrape_time_budget = String(x), "An upper limit - the search stops as soon as every beat has a real choice of footage."));
  } else {
    nodes.push(selectField(T.video_model || "Video model", OPT.video_model, v.video_model, x => v.video_model = x));
    nodes.push(selectField(T.image_model || "Image model", OPT.image_model, v.image_model, x => v.image_model = x));
    nodes.push(el("div", "subcap", "Media"));
    const g = el("div", "fld-grid");
    [["out_video_clips", "Video clips"], ["out_gpt_images", "AI images"], ["out_web_images", "Web images"], ["out_wikimedia", "Wikimedia"]]
      .forEach(([k, lab]) => g.appendChild(toggleField(lab, v[k], x => v[k] = x)));
    nodes.push(g);
  }
  const grp = group("Footage", footageSummary(v), ...nodes); grp.dataset.grp = "footage"; return grp;
}
function directorGroup(v) {
  const nodes = [];
  nodes.push(selectField("Reasoning model", OPT.reasoning_model, v.reasoning_model, (x, initial) => {
    v.reasoning_model = x; v.reasoning_mode = reasoningOptions(x, v.reasoning_mode).value; if (!initial) render();
  }));
  const rm = reasoningOptions(v.reasoning_model, v.reasoning_mode);
  if (rm.options.length) {
    v.reasoning_mode = rm.value;
    nodes.push(selectField("Reasoning mode", rm.options, rm.value, x => v.reasoning_mode = x,
      "Higher reasoning helps on difficult scripts and costs time and money."));
  } else v.reasoning_mode = "";
  const grp = group("Director", labelFor(OPT.reasoning_model, v.reasoning_model), ...nodes); grp.dataset.grp = "director"; return grp;
}
function layersSummary(v) {
  const fx = [v.add_visual_effects !== false ? "arrows" : null].filter(Boolean);
  return `${fx.length ? fx.join("+") : "no VFX"} · sfx ${v.sfx_amount || "medium"} · captions ${v.out_captions ? "on" : "off"}`;
}
function layersGroup(v) {
  const nodes = [];
  nodes.push(el("div", "subcap", "Visual effects"));
  const vg = el("div", "fld-grid");
  vg.appendChild(toggleField("Arrows & callouts", v.add_visual_effects !== false, x => v.add_visual_effects = x));
  nodes.push(vg);
  nodes.push(amountField("VFX amount", v.vfx_amount || "medium", x => v.vfx_amount = x));
  nodes.push(el("div", "subcap", "Sound"));
  if (isClip()) {
    nodes.push(toggleField("Hook riser & cut SFX", v.out_transition_sfx, x => v.out_transition_sfx = x,
      "Only quiet whoosh, swish, pop and click accents. No ambience or content SFX on real footage."));
  } else {
    const sg = el("div", "fld-grid");
    sg.appendChild(toggleField("Sound effects", v.out_sfx, x => v.out_sfx = x));
    sg.appendChild(toggleField("Transition SFX", v.out_transition_sfx, x => v.out_transition_sfx = x));
    nodes.push(sg);
  }
  nodes.push(amountField("SFX amount", v.sfx_amount || "medium", x => v.sfx_amount = x));
  nodes.push(el("div", "subcap", "Captions"));
  nodes.push(toggleField("Word-by-word captions", v.out_captions, x => { v.out_captions = x; render(); }));
  const grp = group("Layers", layersSummary(v), ...nodes); grp.dataset.grp = "layers"; return grp;
}
const RUN_MODES = [["normal", "Normal run"], ["recut_existing_only", "Recut existing"], ["recut_new_web_images", "New web images"],
  ["recut_regenerate_seedance", "Regenerate clips"], ["recut_recreate_speaker_clip", "Recreate hook"]];
function runGroup(v) {
  const nodes = [];
  if (!isDiscovery()) {
    nodes.push(toggleField(T.halt_after_speech || "Halt after speech generation", v.halt_after_speech, x => v.halt_after_speech = x,
      "Pause to approve or redo the voice before footage is searched. The wait screen tells you when."));
  } else nodes.push(el("div", "hint", "Discovery pauses once, when it has found candidates and needs your pick."));
  if (v.loaded_project_source) {
    nodes.push(el("div", "subcap", "Loaded project"));
    const row = el("div", "fld row");
    row.appendChild(el("span", "lbl", esc(S.projectTitle || v.loaded_project_source)));
    row.appendChild(btn("Unload", () => { v.loaded_project_source = ""; v.loaded_project_mode = "normal"; S.projectSlug = ""; S.projectTitle = ""; persist(); render(); }, "sm ghost"));
    nodes.push(row);
    nodes.push(selectField("What to redo", RUN_MODES.map(([a, b]) => ({ value: a, label: b })), v.loaded_project_mode || "normal", x => v.loaded_project_mode = x));
  }
  const grp = group("Run", v.halt_after_speech && !isDiscovery() ? "pauses after the voice" : "runs through", ...nodes);
  grp.dataset.grp = "run"; return grp;
}
function renderGroupSums() {
  if (!["clip", "ai"].includes(S.mode) || S.view !== "home") return;
  const v = V();
  const sums = { footage: footageSummary(v), director: labelFor(OPT.reasoning_model, v.reasoning_model), layers: layersSummary(v),
    run: v.halt_after_speech && !isDiscovery() ? "pauses after the voice" : "runs through" };
  document.querySelectorAll(".grp[data-grp]").forEach(g => {
    const s = g.querySelector(".grp-head .sum"); const txt = sums[g.dataset.grp] || "";
    if (s && s.textContent !== txt) { s.textContent = txt; flash(s); }
  });
}
function flash(node) {
  if (REDUCED) return;
  node.classList.remove("commit"); void node.offsetWidth; node.classList.add("commit");
}

/* ---- dock: just the start button and whatever is blocking it. The settings recap that used
   to live here repeated the group headers, so it is gone. */
function runProblem() {
  const v = V();
  if (isOthers() || isDiscovery()) return "";
  if (!(v.script || "").trim() && !(isClip() && (v.gen_topic || "").trim())) return "The script box is empty.";
  return "";
}
function renderDock() {
  if (S.view !== "home") return;
  // every other program paints its own status line when a value commits
  if (S.mode === "longform") { renderLongformDock(); return; }
  if (S.mode === "physics") { renderPhysicsDock(); return; }
  if (S.mode === "enhance") { renderEnhanceDock(); return; }
  if (S.mode === "reddit") return;
  const dock = $("v3-dock"); dock.hidden = false;
  let note = dock.querySelector(".dock-note");
  const problem = runProblem();
  if (!note) { dock.innerHTML = ""; note = el("span", "dock-note"); dock.appendChild(note); }
  note.textContent = problem; note.hidden = !problem;
  let go = dock.querySelector(".btn.primary");
  if (!go) {
    dock.appendChild(el("span", "dock-key", "Ctrl+↵"));
    go = btn(T.create_short || "Create Short", submitRun, "primary"); go.id = "go"; dock.appendChild(go);
  }
  go.title = problem || "Ctrl+Enter in the script box also starts the run";
}

/* ------------------------------------------------------------------ payload adapter (/run) */
function buildRunForm() {
  const v = V();
  const fd = new FormData();
  const A = MAN.run.always;
  Object.keys(A).forEach(k => fd.append(k, A[k]));
  MAN.run.state_hidden.forEach(k => fd.append(k, v[k] ? "on" : ""));
  MAN.run.text.forEach(k => fd.append(k, v[k] != null ? String(v[k]) : ""));
  MAN.run.check.forEach(k => { if (v[k]) fd.append(k, "on"); });
  if (isDiscovery() && v.discovery_keep_original_audio) fd.append("discovery_keep_original_audio", "on");
  // The V4 discovery provider and its region are read by the pipeline (agent_core reads both
  // form keys) but the classic shell never posted them - its dropdown was decorative.
  if (isClip()) {
    fd.append("v4_tiktok_discovery_provider", v.v4_tiktok_discovery_provider || "scrapedo");
    fd.append("v4_scrapedo_geo", v.v4_scrapedo_geo || "jp");
  }
  if (FILES.speaker_image_file) fd.append("speaker_image_file", FILES.speaker_image_file);
  if (FILES.motion_loop_first_frame) fd.append("motion_loop_first_frame", FILES.motion_loop_first_frame);
  return fd;
}
function prepareRunValues() {
  const v = V();
  if (isClip()) clipInvariants(v);
  else { v.clip_source = "generate"; v.culture_facts_mode = false; v.visuals_from_script_mode = true; }
  const text = (v.script || "").trim();
  v.script = (isDiscovery() || isOthers()) ? "" : text;
  if (v.hook_text && !hookWords(v.script).includes(hookWords(v.hook_text))) v.hook_text = "";
  if (v.impact_word && !v.script.toLowerCase().includes(v.impact_word.toLowerCase())) v.impact_word = "";
  try {
    const folded = v.script.toLocaleLowerCase();
    const valid = JSON.parse(v.hook_keywords || "[]").filter(k => String(k || "").trim() && folded.includes(String(k).trim().toLocaleLowerCase()));
    v.hook_keywords = JSON.stringify(valid.slice(0, 14));
  } catch (e) { v.hook_keywords = "[]"; }
  resetTtsVoice(v);
}
let submitting = false;
async function submitRun() {
  if (submitting) return;
  const problem = runProblem();
  if (problem) { toast(problem, true); const box = $("script-box"); if (box) box.focus(); return; }
  prepareRunValues();
  submitting = true;
  const go = $("go"); if (go) { go.disabled = true; go.textContent = "Starting…"; }
  askNotifyPermission();
  try {
    const r = await fetch("/run", { method: "POST", body: buildRunForm() });
    const jid = new URL(r.url, location.href).searchParams.get("id");
    if (!jid) throw new Error("no job id");
    // Custom search terms belong to the run that was just sent, not to the next one.
    V().scrape_terms = "";
    startJob(jid, "run");
  } catch (e) {
    toast("The run could not start: " + e, true);
    if (go) { go.disabled = false; go.textContent = T.create_short || "Create Short"; }
  }
  submitting = false;
}

/* ---------------------------------------------------------------- loading a project */
async function loadProject(slug, info) {
  try {
    const d = await jget("/project-preset?slug=" + encodeURIComponent(slug));
    const st = d.state || {};
    const mode = String(st.clip_source || "").toLowerCase() === "scrape" ? "clip" : "ai";
    S.mode = mode; const v = V();
    Object.keys(st).forEach(k => {
      if (k === "scrape_terms") return;              // per run, never inherited from a project
      if (MAN.run.text.includes(k)) v[k] = st[k] != null ? String(st[k]) : v[k];
      if (MAN.run.check.includes(k)) v[k] = !!st[k];
      if (MAN.run.state_hidden.includes(k)) v[k] = !!st[k];
    });
    v.scrape_terms = "";
    v.loaded_project_source = d.slug; v.loaded_project_mode = "normal";
    if (mode === "clip") clipInvariants(v);
    resetTtsVoice(v);
    S.projectSlug = d.slug; S.projectTitle = d.title || d.slug;
    S.view = "home"; persist(); render();
    toast("Loaded " + S.projectTitle + " - choose what to redo in the Run group.");
    window.scrollTo(0, 0);
  } catch (e) { toast("Could not load that project: " + e, true); }
}

/* ------------------------------------------------------------------ enhance (the masters) */
function pickFile(accept, cb) {
  const i = el("input"); i.type = "file"; i.accept = accept;
  i.addEventListener("change", () => { if (i.files && i.files[0]) cb(i.files[0]); });
  i.click();
}

/* ------------------------------------------------------------------ Challenge Station */
function challengeCopy(text, label) {
  navigator.clipboard.writeText(String(text || "")).then(() => toast(label || "Copied"))
    .catch(() => toast("Could not copy to the clipboard.", true));
}
/* True while step 2 holds the premise the user typed instead of one of the generated cards. */
function challengeOwnTopic(C) {
  return C.topics.length === 1 && !!(C.topics[0] || {}).custom;
}
function renderChallenge(main) {
  const C = S.challenge;
  const page = el("div", "page challenge-page");
  page.appendChild(pageHead("Challenge Station",
    "Build the story and approve its voice before any video is generated.",
    "Character → 20 ideas → Seed voice → Approval → 8 generations + edit"));

  const status = el("div", "challenge-status");
  const steps = [[1, "Character"], [2, "Choose idea"], [3, "Script + Seed voice"],
    [4, "Approve voice"], [5, "Generate + edit"]];
  const active = C.result ? 4 : (C.topics.length ? 2 : 1);
  steps.forEach(([n, label]) => {
    const item = el("div", "challenge-step" + (n === active ? " active" : "") + (n < active ? " done" : ""));
    item.innerHTML = `<b>${n < active ? "✓" : n}</b><span>${esc(label)}</span>`;
    status.appendChild(item);
  });
  page.appendChild(status);

  const setup = el("section", "panel challenge-setup");
  const sh = el("div", "panel-head"); sh.appendChild(el("span", "label", "1 · Main character"));
  sh.appendChild(el("span", "cap", "Required · sent as @image1 in every clip")); setup.appendChild(sh);
  const layout = el("div", "challenge-setup-grid");
  const drop = el("div", "drop challenge-drop"); drop.tabIndex = 0; drop.setAttribute("role", "button");
  drop.setAttribute("aria-label", "Upload a character sheet image");
  const file = FILES.challenge_character;
  // The remembered NAME survives a reload, the File object does not. The drop zone used to look
  // identical either way, so the screen showed a filename while step 3 stayed locked with no
  // visible reason. The stale state now says so out loud and is marked.
  const stale = !file && !!C.fileName;
  drop.innerHTML = `<b>${esc(file ? file.name : (C.fileName || "Upload character sheet"))}</b>`
    + `<span class="cap">${file ? "Ready for every Seedance request"
        : (stale ? "Not loaded in this session - click to pick the file again"
                 : "PNG, JPG or WebP · front, side and three-quarter views work best")}</span>`;
  if (stale) drop.classList.add("stale");
  if (file) {
    const img = el("img"); img.alt = "Selected character sheet"; img.src = URL.createObjectURL(file); drop.appendChild(img);
  }
  const accept = "image/png,image/jpeg,image/webp,.png,.jpg,.jpeg,.webp";
  const choose = () => pickFile(accept, onCharacter);
  const onCharacter = chosen => {
    if (!chosen) return;
    if (!/^image\/(png|jpeg|webp)$/i.test(chosen.type || "") && !/\.(png|jpe?g|webp)$/i.test(chosen.name || "")) {
      toast("Choose a PNG, JPG or WebP image.", true); return;
    }
    FILES.challenge_character = chosen; C.fileName = chosen.name; C.result = null; persist(); render();
  };
  drop.addEventListener("click", choose);
  drop.addEventListener("keydown", e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); choose(); } });
  ["dragenter", "dragover"].forEach(type => drop.addEventListener(type, e => { e.preventDefault(); drop.classList.add("over"); }));
  ["dragleave", "drop"].forEach(type => drop.addEventListener(type, e => { e.preventDefault(); drop.classList.remove("over"); }));
  drop.addEventListener("drop", e => onCharacter(e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0]));
  layout.appendChild(drop);

  const direction = el("div", "challenge-direction");
  direction.appendChild(el("label", "lbl", "Topic direction (optional)"));
  const ta = el("textarea"); ta.rows = 4; ta.value = C.direction || "";
  ta.placeholder = "e.g. ancient Japan meets normal modern errands, dry humor, no celebrities";
  ta.setAttribute("aria-label", "Optional topic direction");
  ta.addEventListener("input", () => {
    // A direction steers a generated set, so editing it drops those cards - but it must never
    // throw away a premise the user typed themselves.
    if (ta.value !== C.direction && C.topics.length && !challengeOwnTopic(C)) {
      C.topics = []; C.selected = -1; C.result = null;
    }
    C.direction = ta.value; persist();
  });
  direction.appendChild(ta);
  direction.appendChild(el("p", "hint", "Leave it empty for a broad mix of historical visitors, survival days and challenges."));
  const ideasBtn = btn(C.topics.length ? "Craft 20 new topics" : "Craft 20 topics", async () => {
    ideasBtn.disabled = true; ideasBtn.textContent = "Crafting 20 topics…";
    try {
      const d = await jpost("/challenge-topics", { direction: C.direction.trim() });
      if (!d.ok || !Array.isArray(d.topics)) throw new Error(d.error || "No topics returned");
      C.topics = d.topics; C.selected = -1; C.result = null; persist(); render();
      toast("20 topics ready — choose one.");
    } catch (e) {
      ideasBtn.disabled = false; ideasBtn.textContent = C.topics.length ? "Craft 20 new topics" : "Craft 20 topics";
      toast("Topic generation failed: " + e.message, true);
    }
  }, "primary");
  direction.appendChild(ideasBtn);

  // The escape hatch the generated set can never provide: the user already knows the premise, so
  // step 2 is skipped and their exact words go to the writer (POST /challenge-preview, own_topic).
  const own = el("div", "challenge-own");
  own.appendChild(el("label", "lbl", "Or set the topic yourself"));
  const ownRow = el("div", "challenge-own-row");
  const ownInput = el("input"); ownInput.type = "text"; ownInput.maxLength = 160;
  ownInput.value = C.ownTopic || "";
  ownInput.placeholder = "e.g. I raced a Mongol horse archer to a Formula 1 pit stop";
  ownInput.setAttribute("aria-label", "Your own topic");
  ownInput.addEventListener("input", () => { C.ownTopic = ownInput.value; persist(); });
  const useOwn = () => {
    const title = (C.ownTopic || "").trim();
    if (!title) { toast("Type your topic first.", true); ownInput.focus(); return; }
    C.topics = [{ title, hook: "Written by you - the writer gets it word for word.",
                  format: "yours", custom: true }];
    C.selected = 0; C.result = null; persist(); render();
    toast("Your topic is set - write the script next.");
  };
  ownInput.addEventListener("keydown", e => {
    if (e.key === "Enter") { e.preventDefault(); useOwn(); }
  });
  ownRow.appendChild(ownInput);
  ownRow.appendChild(btn("Use this topic", useOwn, "ghost"));
  own.appendChild(ownRow);
  own.appendChild(el("p", "hint", "Skips the 20 ideas: your exact title goes to the writer, nothing renamed."));
  direction.appendChild(own);

  layout.appendChild(direction); setup.appendChild(layout); page.appendChild(setup);

  if (C.topics.length) {
    const ownOnly = challengeOwnTopic(C);
    const ideas = el("section", "panel challenge-ideas");
    const ih = el("div", "panel-head");
    ih.appendChild(el("span", "label", ownOnly ? "2 · Your own topic" : "2 · Choose one topic"));
    ih.appendChild(el("span", "cap", ownOnly ? "Used word for word"
      : `${C.topics.length} distinct directions`));
    ideas.appendChild(ih);
    const grid = el("div", "challenge-topic-grid");
    C.topics.forEach((topic, index) => {
      const card = el("button", "challenge-topic" + (C.selected === index ? " selected" : ""));
      card.type = "button"; card.setAttribute("aria-pressed", String(C.selected === index));
      card.innerHTML = `<small>${String(index + 1).padStart(2, "0")} · ${esc(topic.format || "story")}</small>`
        + `<b>${esc(topic.title)}</b><span>${esc(topic.hook || "")}</span>`;
      card.addEventListener("click", () => { C.selected = index; C.result = null; persist(); render(); });
      grid.appendChild(card);
    });
    ideas.appendChild(grid);
    const selected = C.topics[C.selected];
    const action = el("div", "challenge-plan-action");
    // The button is gated on both earlier steps, so this row has to name the one that is missing:
    // a greyed-out button with no reason next to it reads as a broken screen.
    action.appendChild(el("span", "hint", !FILES.challenge_character
      ? "Locked — step 1 still needs the character sheet: every clip uses it as @image1."
      : (!selected ? "Locked — pick one topic card above to continue."
                   : `${selected.custom ? "Your topic" : "Selected"}: ${selected.title}`)));
    const previewBtn = btn("Write script + Seed voice", async () => {
      if (!FILES.challenge_character || !selected) return;
      previewBtn.disabled = true; previewBtn.textContent = "Writing script + Seed voice…";
      try {
        const fd = new FormData();
        fd.append("character_sheet", FILES.challenge_character);
        fd.append("direction", C.direction || "");
        if (selected.custom) fd.append("own_topic", selected.title);
        else fd.append("topic", JSON.stringify(selected));
        const response = await fetch("/challenge-preview", { method: "POST", body: fd });
        const d = await response.json();
        if (!d.ok) throw new Error(d.error || "No preview returned");
        C.result = d; persist(); render(); toast("Seed voice ready. Listen and approve it before video generation.");
      } catch (e) {
        previewBtn.disabled = false; previewBtn.textContent = "Write script + Seed voice";
        toast("Preview generation failed: " + e.message, true);
      }
    }, "primary");
    previewBtn.disabled = !selected || !FILES.challenge_character;
    action.appendChild(previewBtn); ideas.appendChild(action); page.appendChild(ideas);
  }

  if (C.result) page.appendChild(challengeResult(C.result));
  main.appendChild(page);
}

function challengeResult(result) {
  const wrap = el("section", "challenge-results");
  const banner = el("div", "challenge-dry-banner");
  banner.innerHTML = `<b>VOICE APPROVAL REQUIRED</b><span>Listen first. Timestamps, final prompts and video generation stay locked until you approve this take.</span>`;
  wrap.appendChild(banner);

  const script = panel("3 · Script + Seed voice");
  const meta = el("div", "challenge-result-meta");
  meta.innerHTML = `<b>${esc(result.title)}</b><span>${esc(result.model)}</span>`; script.appendChild(meta);
  const ta = el("textarea", "challenge-script"); ta.readOnly = true; ta.value = result.script || "";
  ta.setAttribute("aria-label", "Generated voiceover script"); script.appendChild(ta);
  if (result.voice_direction) script.appendChild(el("p", "hint", result.voice_direction));
  if (result.voiceover) {
    const voice = el("div", "challenge-voiceover");
    const vo = result.voiceover;
    const duration = Number(vo.duration || 0);
    const speed = Number(vo.speed || 0), vol = Number(vo.volume || 0);
    const tuning = [speed ? `${speed}× speed` : "", vol ? `vol ${vol}` : ""].filter(Boolean).join(" · ");
    voice.innerHTML = `<div><small>SEED TTS MASTER</small><b>${esc(vo.voice || "tim_en")}</b>`
      + `<span>${duration.toFixed(2)}s · ${esc(vo.model || "Seed Speech")}${tuning ? ` · ${esc(tuning)}` : ""}</span></div>`;
    if (vo.url) {
      const audio = el("audio"); audio.controls = true; audio.preload = "metadata"; audio.src = vo.url; voice.appendChild(audio);
    }
    script.appendChild(voice);
  }
  const scriptActions = el("div", "actions");
  scriptActions.appendChild(btn("Copy script", () => challengeCopy(result.script, "Script copied"), "sm"));
  scriptActions.appendChild(el("span", "cap", `Saved: ${esc(result.saved_to || "preview JSON")}`));
  script.appendChild(scriptActions); wrap.appendChild(script);

  const drafts = panel("4 · Approve voice");
  drafts.appendChild(el("p", "hint", "The eight story beats below are drafts. Approval first aligns the spoken words, then rewrites every prompt around its exact edit window. No Day 7 is created."));
  (result.clips || []).slice(0, 8).forEach((clip, index) => {
    const details = el("details", "challenge-request"); if (index === 0) details.open = true;
    const summary = el("summary");
    summary.innerHTML = `<span>${String(index + 1).padStart(2, "0")}</span><b>${esc(clip.label)}</b><small>10s source · timing pending approval</small>`;
    details.appendChild(summary);
    if (clip.narration) details.appendChild(el("blockquote", "", esc(clip.narration)));
    const pre = el("pre", "challenge-json", esc(clip.prompt || "")); details.appendChild(pre);
    details.appendChild(btn("Copy draft prompt", () => challengeCopy(clip.prompt, "Draft prompt copied"), "sm"));
    drafts.appendChild(details);
  });
  const approve = el("div", "challenge-plan-action");
  approve.appendChild(el("span", "hint", "Approval starts exactly 8 × 10s Seedance 2.5 Turbo generations at 720p, 9:16, with sound, then edits them to this voice. Estimated generation cost: $16."));
  const approveBtn = btn("Approve voice & generate short", async () => {
    approveBtn.disabled = true; approveBtn.textContent = "Starting timestamps…";
    try {
      const response = await fetch("/challenge-generate", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ approved: true, preview: result })
      });
      const d = await response.json();
      if (!d.ok || !d.job_id) throw new Error(d.error || "Generation did not start");
      startJob(d.job_id, "challenge");
    } catch (e) {
      approveBtn.disabled = false; approveBtn.textContent = "Approve voice & generate short";
      toast("Generation could not start: " + e.message, true);
    }
  }, "primary");
  approve.appendChild(approveBtn); drafts.appendChild(approve); wrap.appendChild(drafts);
  return wrap;
}

function renderEnhance(main) {
  const E = S.enhance; const M = E.master;
  const page = el("div", "page");
  if (pendingDraft) page.appendChild(restoreBanner());
  const head = el("div", "page-head");
  head.appendChild(el("h1", "h1", "Effects Station"));
  head.appendChild(el("span", "muted", "Sound, arrows, captions or ASMR mastering on an MP4 you already have."));
  page.appendChild(head);
  const grid = el("div", "run-grid");
  const kinds = [["sfx", "Sound effects"], ["visual", "Visual effects"], ["captions", "Captions"], ["asmr", "ASMR sound"], ["actionedit", "Action Edit"]];
  if (E.kind === "actionedit") {
    // the one master that takes three clips: its own panel and its own settings
    grid.appendChild(actionPanel());
    const groups = el("div", "groups");
    groups.appendChild(group("What to add", "Action Edit", segField("", kinds, E.kind, x => { E.kind = x; history.replaceState(null, "", routeFor("enhance")); })));
    const ag = actionGroups(); [...ag.children].forEach(g => groups.appendChild(g));
    grid.appendChild(groups);
    page.appendChild(grid);
    main.appendChild(page);
    renderEnhanceDock();
    return;
  }
  const left = el("section", "panel");
  const ph = el("div", "panel-head"); ph.appendChild(el("span", "label", "Video")); left.appendChild(ph);
  const drop = el("div", "drop"); drop.tabIndex = 0; drop.setAttribute("role", "button");
  const f = FILES.master_video;
  drop.innerHTML = `<b>${f ? esc(f.name) : "Attach a video"}</b><span class="cap">${f ? "Click to pick another" : "Click or drop an MP4, MOV or WebM"}</span>`;
  if (f) { const vid = el("video"); vid.controls = true; vid.muted = true; vid.src = URL.createObjectURL(f); drop.appendChild(vid); }
  else if (E.fileName) drop.appendChild(el("div", "hint", "Last time: " + esc(E.fileName) + " - the file itself does not survive a reload, attach it again."));
  const accept = "video/mp4,video/quicktime,video/webm,video/x-matroska,.mp4,.mov,.webm,.mkv";
  const onFile = (file) => { if (!file) return; FILES.master_video = file; E.fileName = file.name; persist(); render(); };
  drop.addEventListener("click", e => { if (e.target.tagName !== "VIDEO") pickFile(accept, onFile); });
  drop.addEventListener("keydown", e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); pickFile(accept, onFile); } });
  ["dragenter", "dragover"].forEach(ev => drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.add("over"); }));
  ["dragleave", "drop"].forEach(ev => drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.remove("over"); }));
  drop.addEventListener("drop", e => { const file = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0]; onFile(file); });
  left.appendChild(drop);
  grid.appendChild(left);

  const groups = el("div", "groups");
  groups.appendChild(group("What to add", kinds.find(k => k[0] === E.kind)[1], segField("", kinds, E.kind, x => { E.kind = x; history.replaceState(null, "", routeFor("enhance")); })));
  const nodes = [];
  if (E.kind === "sfx") {
    nodes.push(selectField(T.planning_agent || "Planning agent", OPT.master_reasoning, M.reasoning_model, (x, initial) => { M.reasoning_model = x; M.reasoning_mode = reasoningOptions(x, M.reasoning_mode).value; if (!initial) render(); }));
    const rm = reasoningOptions(M.reasoning_model || firstVal(OPT.master_reasoning), M.reasoning_mode);
    if (rm.options.length) { M.reasoning_mode = rm.value; nodes.push(selectField("Reasoning mode", rm.options, rm.value, x => M.reasoning_mode = x)); }
    nodes.push(amountField(T.sfx_amount || "SFX amount", M.sfx_amount || "medium", x => M.sfx_amount = x));
  } else if (E.kind === "visual") {
    nodes.push(selectField(T.analysis_agent || "Analysis agent", OPT.vfx_reasoning, M.reasoning_model, (x, initial) => { M.reasoning_model = x; M.reasoning_mode = reasoningOptions(x, M.reasoning_mode).value; if (!initial) render(); }));
    const rm = reasoningOptions(M.reasoning_model || firstVal(OPT.vfx_reasoning), M.reasoning_mode);
    if (rm.options.length) { M.reasoning_mode = rm.value; nodes.push(selectField("Reasoning mode", rm.options, rm.value, x => M.reasoning_mode = x)); }
    nodes.push(amountField(T.effect_amount || "Effect amount", M.vfx_amount || "medium", x => M.vfx_amount = x));
  } else if (E.kind === "captions") {
    nodes.push(selectField(T.cap_max_words || "Max words per caption", OPT.caption_max_words, M.caption_max_words, x => M.caption_max_words = x));
    nodes.push(selectField(T.cap_center_y || "Caption vertical position", OPT.caption_center_y, M.caption_center_y, x => M.caption_center_y = x));
  } else {
    nodes.push(selectField("Sound character", [
      { value: "close", label: "Close-up ASMR - vivid texture and movement" },
      { value: "natural", label: "Natural detail - clear but realistic" },
      { value: "soft", label: "Soft & calm - gentle mechanical ambience" }], M.asmr_profile || "close", x => M.asmr_profile = x,
      "Uses only the video's original sound. Nothing is generated or added."));
  }
  groups.appendChild(group("Settings", "", ...nodes));
  grid.appendChild(groups);
  page.appendChild(grid);
  main.appendChild(page);
  renderEnhanceDock();
}
function renderEnhanceDock() {
  const E = S.enhance;
  const kinds = { sfx: "Sound effects", visual: "Visual effects", captions: "Captions", asmr: "ASMR sound", actionedit: "Action Edit" };
  if (E.kind === "actionedit") {
    const n = ACTION_FILES.filter(Boolean).length;
    const go = btn(n >= 3 ? "Build the edit" : `Build the edit (${n}/3 clips)`, submitActionEdit);
    dockWith([["Clips", `${n}/3`], ["Title", S.action.title || "untitled"], ["Ramps", S.action.ramps || "2"]], go, n < 3 ? "Attach all three clips." : "");
    go.disabled = n < 3;
    return;
  }
  const f = FILES.master_video;
  const labels = { sfx: T.add_sfx || "Add sound effects", visual: T.add_arrows || "Add arrows", captions: T.add_captions || "Add captions", asmr: "Master ASMR sound" };
  const go = btn(labels[E.kind], () => submitMaster(E.kind));
  dockWith([["Video", f ? f.name : "none attached"], ["Add", kinds[E.kind]]], go, f ? "" : "Attach a video first.");
  go.disabled = !f;
}
async function submitMaster(kind) {
  const man = MAN.masters[kind]; const M = S.enhance.master;
  if (!FILES.master_video) { toast("Attach a video first.", true); return; }
  const fd = new FormData();
  fd.append(man.file, FILES.master_video);
  if (kind === "sfx") {
    fd.append("reasoning_model", M.reasoning_model || firstVal(OPT.master_reasoning));
    fd.append("reasoning_mode", M.reasoning_mode || "");
    fd.append("sfx_amount", M.sfx_amount || "medium");
  } else if (kind === "visual") {
    fd.append("reasoning_model", M.reasoning_model || firstVal(OPT.vfx_reasoning));
    fd.append("reasoning_mode", M.reasoning_mode || "");
    fd.append("vfx_amount", M.vfx_amount || "medium");
  } else if (kind === "captions") {
    fd.append("caption_max_words", M.caption_max_words || firstVal(OPT.caption_max_words) || "4");
    fd.append("caption_center_y", M.caption_center_y || firstVal(OPT.caption_center_y) || "0.62");
  } else {
    fd.append("asmr_profile", M.asmr_profile || "close");
  }
  const go = $("go"); if (go) { go.disabled = true; go.textContent = "Uploading…"; }
  askNotifyPermission();
  try {
    const r = await fetch(man.action, { method: "POST", body: fd });
    const jid = new URL(r.url, location.href).searchParams.get("id");
    if (!jid) throw new Error("no job id");
    startJob(jid, { sfx: "sfx", visual: "visual", captions: "caption", asmr: "asmr" }[kind]);
  } catch (e) { toast((T.upload_failed || "Upload failed") + " " + e, true); render(); }
}

/* ------------------------------------------------------------------ the wait screen */
let pollTimer = null, tickTimer = null, lastPoll = null, prevStatus = "", jobStartedAt = 0, notified = new Set();
function startJob(jobId, kind) {
  S.jobId = jobId; S.jobStatus = "running"; S.jobKind = kind || "run"; S.view = "wait";
  prevStatus = ""; lastPoll = null; notified = new Set();
  history.replaceState(null, "", withUi("/job?id=" + encodeURIComponent(jobId)));
  persist(); render();
}
function openJob(jobId) { startJob(jobId, S.jobKind || "run"); }
function stopPolling() {
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  if (tickTimer) { clearInterval(tickTimer); tickTimer = null; }
}
function renderWait(main) {
  const w = el("div", "wait"); w.id = "wait";
  const head = el("div", "wait-head");
  const title = el("div", "title");
  title.appendChild(el("span", "label", "Run"));
  const h = el("h1", "h1"); h.id = "wait-title"; h.textContent = runTitle(); title.appendChild(h);
  const st = el("div", "status"); st.id = "wait-status"; st.innerHTML = '<i aria-hidden="true"></i><span>Starting…</span>'; title.appendChild(st);
  head.appendChild(title);
  const clk = el("div", "clock");
  clk.appendChild(el("div", "big mono", "0:00")).id = "wait-clock";
  clk.appendChild(el("div", "cap", "")).id = "wait-sub";
  head.appendChild(clk);
  w.appendChild(head);
  const rail = el("div", "rail"); rail.id = "rail"; rail.setAttribute("aria-label", "Pipeline steps"); w.appendChild(rail);
  const health = el("div", "health"); health.id = "health"; health.hidden = true; w.appendChild(health);
  const act = el("div", "activity mono"); act.id = "activity"; act.setAttribute("aria-live", "polite"); w.appendChild(act);
  const actions = el("div", "wait-actions"); actions.id = "wait-actions"; w.appendChild(actions);
  const gate = el("div"); gate.id = "gate"; w.appendChild(gate);
  const fin = el("div"); fin.id = "final"; w.appendChild(fin);
  main.appendChild(w);
  pollJob(); pollTimer = setInterval(pollJob, 2500);
  tickTimer = setInterval(tickClock, 1000);
}
function runTitle() {
  const masters = { sfx: "Sound effects", visual: "Visual effects", caption: "Captions", asmr: "ASMR sound" };
  if (masters[S.jobKind]) return masters[S.jobKind] + (S.enhance.fileName ? " · " + S.enhance.fileName : "");
  if (S.jobKind === "actionedit") return "Action Edit" + (S.action.title ? " · " + S.action.title : "");
  if (S.jobKind === "longform") return (S.longform.script || "").split(/\n/)[0].trim().slice(0, 90) || S.longform.title || "Sketch Explainer";
  if (S.jobKind === "physics") { const sc = physSceneOf(S.physics); return "Physics · " + (sc ? sc.title : (S.physics.prompt || "").slice(0, 80) || "simulation"); }
  if (S.jobKind === "reddit") return "Reddit story" + (S.reddit.story && S.reddit.story.title ? " · " + S.reddit.story.title.slice(0, 70) : "");
  const v = V();
  const first = (v.script || v.gen_topic || v.others_action || "").split(/\n/)[0].trim();
  return first ? first.slice(0, 90) : (S.projectTitle || (isClip() ? "Clip Short" : "AI Short"));
}
function tickClock() {
  if (!jobStartedAt) return;
  const c = $("wait-clock"); if (c) c.textContent = clock(nowSec() - jobStartedAt);
  if (lastPoll && lastPoll.status === "running") setTitle(`${clock(nowSec() - jobStartedAt)} · ${(lastPoll.v3 || {}).phase || "running"} — Shortslab`);
}
async function pollJob() {
  if (!S.jobId) return;
  let d;
  // &v3=1 asks for the additive shell-v3 block (steps + typical durations + run health). The
  // classic shells poll without it and get the byte-identical payload they always got.
  try { d = await jget("/job-status?id=" + encodeURIComponent(S.jobId) + "&v3=1"); } catch (e) { return; }
  if (!d.exists) {
    stopPolling(); S.jobStatus = "missing";
    paintStatus("missing", T.err_no_job || "This job does not exist anymore.");
    // Nothing is running, so the step rail and the activity line are an empty ruled box and a
    // blinking cursor waiting for output that will never come. Take them off the screen.
    ["rail", "activity", "wait-actions"].forEach(id => { const n = $(id); if (n) n.hidden = true; });
    const clock = $("wait-clock"); if (clock) clock.textContent = "--:--";
    persist(); return;
  }
  lastPoll = d;
  const x = d.v3 || {};
  if (d.created_at) jobStartedAt = +d.created_at;
  if (d.job_kind && d.job_kind !== S.jobKind) S.jobKind = d.job_kind;
  const t = $("wait-title"); if (t && t.textContent !== runTitle()) t.textContent = runTitle();
  if (d.status !== S.jobStatus) {
    S.jobStatus = d.status; persist(); renderTop();
    if (prevStatus && prevStatus !== d.status) announce(d);
  }
  prevStatus = d.status;
  paintRail(x, d);
  paintHealth(x.health || {});
  const act = $("activity"); if (act && act.textContent !== (x.activity || "")) act.textContent = x.activity || "";
  paintActions(d);
  paintGate(d);
  tickClock();
  const sub = $("wait-sub");
  if (sub) {
    const started = jobStartedAt ? new Date(jobStartedAt * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : "";
    const typ = x.typical_total ? ` · typically ${roughMinutes(x.typical_total)}` : "";
    const remain = (x.typical_total && d.status === "running") ? ` · about ${roughMinutes(x.typical_total * (1 - (x.progress || 0)))} left` : "";
    sub.textContent = (started ? "started " + started : "") + typ + remain;
  }
  const wrap = $("wait");
  if (wrap) wrap.className = "wait " + ({ awaiting_approval: "paused", error: "error", cancelled: "error", done: "done" })[d.status] || "";
  const label = { running: "Running" + (x.phase ? " · " + x.phase : ""), cancelling: "Cancelling…", awaiting_approval: "Paused · needs you",
    done: "Finished", error: "Stopped with an error", cancelled: "Cancelled" }[d.status] || d.status;
  paintStatus(d.status, label);
  setFavicon({ status: d.status, progress: x.progress || 0 });
  if (d.status === "awaiting_approval") setTitle("Needs you · " + runTitle() + " — Shortslab");
  else if (d.status === "done") setTitle("Finished · " + runTitle() + " — Shortslab");
  else if (["error", "cancelled"].includes(d.status)) setTitle("Stopped · " + runTitle() + " — Shortslab");
  if (["done", "error", "cancelled"].includes(d.status)) { stopPolling(); paintFinal(d); }
}
function paintStatus(status, label) {
  const st = $("wait-status"); if (!st) return;
  const span = st.querySelector("span"); if (span && span.textContent !== label) span.textContent = label;
  // The stylesheet has always had .wait.error / .wait.done / .wait.paused - nothing ever set
  // them, so a failed run and a finished one both showed the breathing green dot of a run in
  // progress. The status is the argument; use it.
  const wait = $("wait"); if (!wait) return;
  const state = ({ error: "error", missing: "error", cancelled: "error",
                   done: "done", paused: "paused" })[String(status || "")] || "";
  ["error", "done", "paused"].forEach(name => wait.classList.toggle(name, name === state));
}
function stepIcon(state) {
  if (state === "done") return svgIcon("check");
  if (state === "stopped") return svgIcon("x");
  return "";
}
/* The bar is a bar of the VIDEO's time, so it is ruled where the video is cut. Each segment is
   one beat, as wide as that beat is long; the one the fill has reached is the shot being worked
   on and says so. A percentage tells you how far; this tells you WHERE. */
function paintBeats(rail, bar, x) {
  const beats = x.beats || [];
  const total = beats.length ? beats[beats.length - 1].end : 0;
  let ticks = bar.querySelector(".beats");
  if (!beats.length || total <= 0) { if (ticks) ticks.remove(); return; }
  const sig = beats.length + "@" + total;
  if (!ticks || ticks.dataset.sig !== sig) {
    if (ticks) ticks.remove();
    ticks = el("div", "beats"); ticks.dataset.sig = sig; ticks.setAttribute("aria-hidden", "true");
    beats.forEach((b, i) => {
      const seg = el("span", "beat");
      seg.style.flexGrow = String(Math.max(0.001, b.end - b.start));
      seg.title = `${i + 1}/${beats.length} · ${fmtClock(b.start)}–${fmtClock(b.end)}${b.text ? " · " + b.text : ""}`;
      ticks.appendChild(seg);
    });
    bar.appendChild(ticks);
  }
  // Before a single frame is rendered the run is still walking these same beats - cleaning one
  // clip's captions, rebuilding another's frame - and it says which one. That placement beats
  // the render percentage, which is still 0 the whole time the pre-pass runs.
  let current = -1;
  if (typeof x.beat_index === "number" && x.beat_index >= 0) {
    current = Math.min(x.beat_index, beats.length - 1);
  } else {
    const at = (typeof x.progress === "number" ? x.progress : 0) * total;
    beats.forEach((b, i) => { if (at >= b.start) current = i; });
  }
  [...ticks.children].forEach((seg, i) => {
    seg.classList.toggle("done", i < current);
    seg.classList.toggle("now", i === current);
  });
  const nowBeat = beats[current];
  const label = rail.querySelector(".label");
  if (label && nowBeat) {
    const doing = x.beat_doing ? " — " + x.beat_doing : "";
    const text = `Shot ${current + 1} of ${beats.length}${doing}${nowBeat.text ? " · " + nowBeat.text : ""}`;
    if (label.dataset.beat !== text) { label.dataset.beat = text; label.textContent = text; }
  }
}
function fmtClock(s) {
  s = Math.max(0, s || 0);
  return Math.floor(s / 60) + ":" + String(Math.floor(s % 60)).padStart(2, "0");
}
/* What the current step is doing inside itself: "clip 3 of 10", "frames 37 of 63". A phase that
   reports nothing keeps the line hidden rather than showing a guess. */
function paintSub(rail, x) {
  const sub = x.sub;
  let line = rail.querySelector(".subline");
  if (!sub || !sub.total) { if (line) line.remove(); return; }
  if (!line) { line = el("div", "subline"); rail.appendChild(line); }
  const pct = Math.max(0, Math.min(1, sub.done / sub.total));
  const text = `${sub.done} of ${sub.total} ${sub.label}`;
  if (line.dataset.text !== text) {
    line.dataset.text = text;
    line.innerHTML = `<span>${esc(text)}</span><i></i>`;
  }
  const fill = line.querySelector("i");
  if (fill) fill.style.setProperty("--fill", pct);
}
function paintRail(x, d) {
  const rail = $("rail"); if (!rail) return;
  const steps = x.steps || [];
  if (!steps.length) {
    // timeline render / longform: one bar, real percentage when known
    let bar = rail.querySelector(".bar");
    if (!bar) { rail.innerHTML = ""; rail.appendChild(el("div", "label", d.job_kind === "longform" ? "Generating images" : "Rendering")); bar = el("div", "bar"); bar.appendChild(el("i")); rail.appendChild(bar); }
    const known = typeof x.progress === "number" && x.progress > 0;
    bar.classList.toggle("indet", !known);
    bar.style.setProperty("--fill", known ? x.progress : 0);
    paintBeats(rail, bar, x);
    paintSub(rail, x);
    return;
  }
  const maxTyp = Math.max(0, ...steps.map(s => +s.typical || 0));
  const anyTyp = maxTyp > 0;
  if (rail.dataset.n !== String(steps.length)) {
    rail.innerHTML = ""; rail.dataset.n = String(steps.length);
    steps.forEach((s, i) => {
      const row = el("div", "rail-row"); row.dataset.i = i;
      row.innerHTML = `<span class="name"><span class="ico"></span><span class="nm"></span></span>` +
        `<div class="rail-track-wrap"><div class="rail-track"><span class="rail-fill"></span><span class="rail-over"></span></div></div>` +
        `<span class="t"></span><span class="typ"></span>`;
      rail.appendChild(row);
    });
    rail.appendChild(el("div", "rail-foot"));
  }
  steps.forEach((s, i) => {
    const row = rail.children[i]; if (!row) return;
    const typ = +s.typical || 0, elapsed = s.elapsed == null ? null : +s.elapsed;
    const cls = "rail-row " + s.state + ((s.state === "active" && !typ) ? " indet" : "");
    if (row.className !== cls) row.className = cls;
    row.querySelector(".nm").textContent = s.name;
    row.querySelector(".ico").innerHTML = stepIcon(s.state);
    const share = anyTyp ? Math.max(0.12, (typ || maxTyp * 0.25) / maxTyp) : 1;
    row.querySelector(".rail-track").style.setProperty("--share", (share * 100).toFixed(1) + "%");
    let fill = 0, over = 0;
    if (s.state === "done" || s.state === "stopped") { fill = 1; if (typ && elapsed > typ) over = Math.min(1, (elapsed - typ) / typ); }
    else if (s.state === "active" && typ && elapsed != null) { fill = Math.min(1, elapsed / typ); if (elapsed > typ) over = Math.min(1, (elapsed - typ) / typ); }
    row.querySelector(".rail-track").style.setProperty("--fill", fill.toFixed(3));
    row.querySelector(".rail-over").style.setProperty("--over", over.toFixed(3));
    const t = row.querySelector(".t"); t.textContent = elapsed == null ? "" : clock(elapsed);
    const ty = row.querySelector(".typ");
    ty.textContent = typ ? "typ " + clock(typ) : (s.state === "pending" ? "" : "no history yet");
    ty.className = "typ" + (typ && elapsed != null && elapsed > typ * 1.25 ? " over" : "");
    ty.title = typ ? "Median of the last runs of this kind" : "";
  });
  const foot = rail.querySelector(".rail-foot");
  if (foot) {
    const done = steps.filter(s => s.state === "done").length;
    const pct = Math.round((x.progress || 0) * 100);
    foot.innerHTML = `<span>${done} of ${steps.length} steps done</span><span>${anyTyp ? "<b>" + pct + "%</b> of a typical run" : "First run of this kind - typical durations appear from the next one"}</span>`;
  }
}
function paintHealth(h) {
  const box = $("health"); if (!box) return;
  const tiles = [];
  if (h.queries) tiles.push(["queries", h.queries, ""]);
  if (h.checked) {
    tiles.push(["checked", h.checked, ""]);
    const rej = h.rejected || 0;
    tiles.push(["rejected", rej, (h.checked >= 10 && rej / h.checked > 0.85) ? "warn" : ""]);
  }
  if (h.accepted) tiles.push(["accepted", h.accepted, "good"]);
  if (h.beats_total) tiles.push(["beats covered", `${h.beats_covered}/${h.beats_total}`, h.beats_covered === h.beats_total ? "good" : ""]);
  if (h.clips_total) tiles.push(["clips", `${h.clips_done}/${h.clips_total}`, ""]);
  if (h.images_total) tiles.push(["images", `${h.images_done}/${h.images_total}`, ""]);
  box.hidden = !tiles.length;
  if (!tiles.length) return;
  const sig = JSON.stringify(tiles);
  if (box.dataset.sig === sig) return;
  box.dataset.sig = sig;
  box.innerHTML = "";
  tiles.forEach(([k, n, cls]) => {
    const t = el("div", "tile " + cls); t.innerHTML = `<span class="n">${esc(n)}</span><span class="k">${esc(k)}</span>`; box.appendChild(t);
  });
}
function paintActions(d) {
  const a = $("wait-actions"); if (!a) return;
  const live = ["running", "cancelling", "awaiting_approval"].includes(d.status);
  const sig = d.status + "|" + (d.project_slug || "") + "|" + (d.clip_source || "");
  if (a.dataset.sig === sig) return;
  a.dataset.sig = sig; a.innerHTML = "";
  if (live) a.appendChild(btn(d.status === "cancelling" ? "Cancelling…" : "Cancel run", cancelJob, "danger sm"));
  a.appendChild(btn("Technical log", () => {
    const pre = el("pre", "", esc((lastPoll && lastPoll.log_text) || ""));
    const m = modal("Technical log", pre);
    pre.scrollTop = pre.scrollHeight;
    const refresh = setInterval(() => { if (!document.body.contains(m)) { clearInterval(refresh); return; } const txt = (lastPoll && lastPoll.log_text) || ""; if (pre.textContent !== txt) { pre.textContent = txt; pre.scrollTop = pre.scrollHeight; } }, 2500);
  }, "sm"));
  if (d.project_slug && d.clip_source === "scrape") { const l = el("a", "btn sm ghost", "Scrape log"); l.href = "/scrape-log-view?slug=" + encodeURIComponent(d.project_slug); l.target = "_blank"; a.appendChild(l); }
  a.appendChild(el("span", "spacer"));
  if (live) a.appendChild(el("span", "wait-note", "You can leave this tab. The title, the tab icon and one notification will tell you when to come back."));
  else a.appendChild(btn("New run", () => { S.jobId = null; S.jobStatus = ""; goHome(); }, "sm"));
}
async function cancelJob() {
  if (!S.jobId) return;
  if (!confirm("Cancel this run?")) return;
  await fetch("/cancel?id=" + encodeURIComponent(S.jobId), { method: "POST" });
  S.jobStatus = "cancelling"; persist(); pollJob();
}
function paintGate(d) {
  const g = $("gate"); if (!g) return;
  if (d.status !== "awaiting_approval") { if (g.dataset.sig) { g.innerHTML = ""; delete g.dataset.sig; } return; }
  if (paintExtraGates(d, g)) return;      // the longform speech parts, the physics preview frame
  const cands = d.discovery_review || [];
  const sig = d.speech_audio_url ? "speech:" + d.speech_audio_url : cands.length ? "disc:" + cands.length : "";
  if (!sig || g.dataset.sig === sig) return;
  g.dataset.sig = sig; g.innerHTML = "";
  const c = el("section", "gate");
  if (d.speech_audio_url) {
    c.appendChild(el("h2", "h2", "Your voiceover is ready"));
    c.appendChild(el("p", "muted", "Listen, then approve - or redo it with a different voice. The footage search only starts after this."));
    const au = el("audio"); au.controls = true; au.src = d.speech_audio_url; c.appendChild(au);
    const cur = +(d.speech_speed || 0) || 1.15;
    const row = el("div", "row");
    row.appendChild(el("span", "lbl muted", "Narration speed"));
    const ssel = el("select"); ssel.style.width = "auto";
    ssel.appendChild(new Option("Keep current (" + cur.toFixed(2) + "x)", ""));
    ["1.0", "1.15", "1.2", "1.3", "1.4", "1.5", "1.6"].forEach(x => ssel.appendChild(new Option((+x).toFixed(2) + "x", x)));
    ssel.addEventListener("change", () => { const sel = +ssel.value || cur; au.playbackRate = Math.max(0.5, Math.min(2.5, sel / cur)); try { au.currentTime = 0; au.play().catch(() => {}); } catch (e) {} });
    row.appendChild(ssel);
    row.appendChild(btn(svgIcon("check") + " Approve & continue", async () => {
      const q = ssel.value ? "&speed=" + encodeURIComponent(ssel.value) : "";
      await fetch("/approve-speech?id=" + encodeURIComponent(S.jobId) + q, { method: "POST" });
      g.innerHTML = ""; delete g.dataset.sig; pollJob();
    }, "primary"));
    c.appendChild(row);
    if (S.jobKind === "run") {
      const v = V();
      const redo = el("div", "redo");
      redo.appendChild(el("span", "lbl muted", "Not happy? Redo with a different voice"));
      const rr = el("div", "row");
      const msel = el("select"); (OPT.tts_model || []).forEach(o => msel.appendChild(new Option(o.label, o.value)));
      if ([...msel.options].some(o => o.value === v.tts_model)) msel.value = v.tts_model;
      const vsel = el("select");
      const refill = () => { v.tts_model = msel.value; vsel.innerHTML = ""; ttsVoiceOptions(v.tts_model).forEach(o => vsel.appendChild(new Option(o.label, o.value))); resetTtsVoice(v); vsel.value = v.tts_voice; persist(); };
      msel.addEventListener("change", refill); vsel.addEventListener("change", () => { v.tts_voice = vsel.value; persist(); });
      refill();
      rr.appendChild(vsel); rr.appendChild(msel);
      rr.appendChild(btn(svgIcon("play") + " Preview", () => previewTts(v), "sm ghost"));
      rr.appendChild(btn("Generate a new take", async (ev) => {
        ev.currentTarget.disabled = true;
        const body = new URLSearchParams({ speaker_name: v.speaker_name || "Narrator", tts_voice: vsel.value, tts_model: msel.value });
        ["tts_voice_instruction", "tts_language", "tts_native_speed", "tts_volume", "tts_pitch", "tts_sample_rate", "tts_output_format"].forEach(k => { if (v[k] != null) body.set(k, v[k]); });
        try {
          const r = await fetch("/replace-speech?id=" + encodeURIComponent(S.jobId) + "&json=1", { method: "POST", headers: { "Content-Type": "application/x-www-form-urlencoded" }, body });
          const dd = await r.json();
          if (dd && dd.id) { startJob(dd.id, "run"); return; }
        } catch (e) {}
        toast("Could not start a new take.", true); ev.currentTarget.disabled = false;
      }, "sm"));
      redo.appendChild(rr); c.appendChild(redo);
    }
  } else if (cands.length) {
    c.appendChild(el("h2", "h2", "Pick the topic & material"));
    c.appendChild(el("p", "muted", `Discovery found ${cands.length} long source videos. Pick one - only then the script and voiceover are produced.`));
    const grid = el("div", "cands");
    cands.forEach(cd => {
      const k = el("div", "cand");
      k.appendChild(el("b", "", esc(cd.title || "Candidate")));
      k.appendChild(el("span", "cap", esc(`@${cd.author || ""} · ${cd.dur}s · ${(+cd.likes || 0).toLocaleString()} likes · appeal ${cd.appeal}/10`)));
      if (cd.premise) k.appendChild(el("span", "cap", esc(cd.premise)));
      if (cd.video_url) { const vp = el("video"); vp.src = cd.video_url; vp.controls = true; vp.preload = "metadata"; k.appendChild(vp); }
      else if (cd.sheet_url) { const im = el("img"); im.src = cd.sheet_url; im.alt = ""; k.appendChild(im); }
      const ol = el("ol"); (cd.stages || []).slice(0, 6).forEach(s => ol.appendChild(el("li", "", esc(s)))); k.appendChild(ol);
      k.appendChild(btn("Use candidate " + ((+cd.index || 0) + 1), async () => {
        await fetch("/approve-discovery?id=" + encodeURIComponent(S.jobId) + "&action=pick&choice=" + (+cd.index || 0), { method: "POST" });
        g.innerHTML = ""; delete g.dataset.sig; pollJob();
      }, "sm primary"));
      grid.appendChild(k);
    });
    c.appendChild(grid);
  }
  g.appendChild(c);
  c.scrollIntoView({ behavior: REDUCED ? "auto" : "smooth", block: "nearest" });
}
function paintFinal(d) {
  const fin = $("final"); if (!fin || fin.dataset.done) return;
  fin.dataset.done = "1";
  if (d.status === "done") {
    const r = el("section", "result");
    if (d.result_video_url) {
      const vid = el("video"); vid.controls = true; vid.src = d.result_video_url; r.appendChild(vid);
    } else r.classList.add("error"), r.style.borderColor = "var(--amber-line)";
    const body = el("div");
    const isLong = d.job_kind === "longform";
    body.appendChild(el("h2", "h2", d.open_timeline ? "Your edit is ready" : isLong ? "Your explainer is ready" : (T.your_short_ready || "Your Short is ready")));
    body.appendChild(el("p", "muted", d.open_timeline
      ? "The clips are assigned. Open the timeline to fine-tune and render."
      : isLong ? "Download it, or open the project in the Library. The frame editor for a finished explainer still runs in the classic console."
      : "Download it, or open the timeline to change the cut."));
    const acts = el("div", "acts");
    if (d.result_video_url) { const a = el("a", "btn primary", svgIcon("down") + " Download"); a.href = d.result_video_url; a.download = d.result_video_name || "short.mp4"; acts.appendChild(a); }
    if (d.project_slug && (d.open_timeline || !d.result_video_url || d.job_kind === "run")) { const a = el("a", "btn" + (d.open_timeline ? " primary" : ""), "Open timeline"); acts.appendChild(timelineLink(a, d.project_slug)); }
    if (d.project_slug) { const a = el("a", "btn ghost", "Project files"); a.href = "/assets?q=" + encodeURIComponent(d.project_slug); acts.appendChild(a); }
    body.appendChild(acts); r.appendChild(body); fin.appendChild(r);
  } else {
    const r = el("section", "result error");
    r.appendChild(el("h2", "h2", d.status === "cancelled" ? "The run was cancelled" : "The run stopped with an error"));
    const tail = String(d.log_text || "").split("\n").filter(Boolean).slice(-12).join("\n");
    if (d.status !== "cancelled") r.appendChild(el("p", "muted", "The last lines of the log are below; the full log is behind Technical log."));
    if (tail) r.appendChild(el("pre", "", esc(tail)));
    const acts = el("div", "acts");
    acts.appendChild(btn("Back to the run page - settings are kept", () => { S.jobId = null; S.jobStatus = ""; goHome(); }, "primary"));
    r.appendChild(acts); fin.appendChild(r);
  }
}

/* ---- being told, without watching: title, favicon, one notification, a short chime */
function setTitle(t) { if (document.title !== t) document.title = t; }
function setFavicon(state) {
  const link = $("v3-favicon"); if (!link) return;
  if (!state) { if (link.dataset.custom) { link.href = "/favicon.ico"; delete link.dataset.custom; } return; }
  const c = document.createElement("canvas"); c.width = c.height = 32;
  const ctx = c.getContext("2d");
  const colour = { awaiting_approval: "#f3e9d3", error: "#f07a7a", cancelled: "#f07a7a" }[state.status] || "#e9a83b";
  ctx.clearRect(0, 0, 32, 32);
  ctx.fillStyle = "#171514"; ctx.beginPath(); ctx.arc(16, 16, 15, 0, Math.PI * 2); ctx.fill();
  ctx.lineWidth = 4; ctx.strokeStyle = "#3d3733"; ctx.beginPath(); ctx.arc(16, 16, 11, 0, Math.PI * 2); ctx.stroke();
  ctx.strokeStyle = colour; ctx.lineCap = "round";
  const frac = state.status === "done" ? 1 : Math.max(0.03, Math.min(0.99, state.progress || 0));
  ctx.beginPath(); ctx.arc(16, 16, 11, -Math.PI / 2, -Math.PI / 2 + frac * Math.PI * 2); ctx.stroke();
  if (state.status === "done" || state.status === "awaiting_approval") { ctx.fillStyle = colour; ctx.beginPath(); ctx.arc(16, 16, 5, 0, Math.PI * 2); ctx.fill(); }
  if (["error", "cancelled"].includes(state.status)) { ctx.strokeStyle = colour; ctx.lineWidth = 3; ctx.beginPath(); ctx.moveTo(11, 11); ctx.lineTo(21, 21); ctx.moveTo(21, 11); ctx.lineTo(11, 21); ctx.stroke(); }
  link.href = c.toDataURL("image/png"); link.dataset.custom = "1";
}
function askNotifyPermission() {
  try { if ("Notification" in window && Notification.permission === "default") Notification.requestPermission().catch(() => {}); } catch (e) {}
}
function announce(d) {
  const kind = d.status === "done" ? "done" : d.status === "awaiting_approval" ? "needs" : ["error", "cancelled"].includes(d.status) ? "stopped" : "";
  if (!kind || notified.has(kind)) return;
  notified.add(kind);
  const text = { done: "Finished: " + runTitle(), needs: "Needs you: " + runTitle(), stopped: "Stopped: " + runTitle() }[kind];
  try {
    if ("Notification" in window && Notification.permission === "granted") {
      const n = new Notification("Shortslab", { body: text, tag: "shortslab-" + S.jobId, silent: true });
      n.onclick = () => { try { window.focus(); } catch (e) {} n.close(); };
    }
  } catch (e) {}
  chime(kind);
}
let _actx = null;
function chime(kind) {
  if (kind === "stopped") return;
  try {
    _actx = _actx || new (window.AudioContext || window.webkitAudioContext)();
    if (_actx.state === "suspended") _actx.resume();
    const now = _actx.currentTime;
    const notes = kind === "needs" ? [[660, 0], [880, 0.14]] : [[784, 0], [1047, 0.13], [1319, 0.26]];
    notes.forEach(([freq, t]) => {
      const o = _actx.createOscillator(); o.type = "sine"; o.frequency.value = freq;
      const g = _actx.createGain(); const s = now + t;
      g.gain.setValueAtTime(0.0001, s); g.gain.exponentialRampToValueAtTime(0.14, s + 0.02); g.gain.exponentialRampToValueAtTime(0.0001, s + 0.26);
      o.connect(g); g.connect(_actx.destination); o.start(s); o.stop(s + 0.3);
    });
  } catch (e) {}
}

/* ------------------------------------------------------------------ the showroom
   One machine fills the stage. Arrows, drag, wheel and the arrow keys move you along the line -
   a lateral pan, never a zoom. Enter, or a click on the machine, sits you down: the camera flies
   into the screen and the run page is what that screen shows. */
let showroomEl = null, trackEl = null;
/* What every tube shows: this machine's own blue screen. Never the finished videos - those play
   far behind, on the wall, as the backdrop. */
function screenField(m) {
  return '<span class="field">' +
    '<b>SHORTSLAB</b>' +
    '<u>' + esc(m.name.toUpperCase()) + '</u>' +
    '<i>READY<span class="car"></span></i>' +
  '</span>';
}
const GLASS_X = 0.675;        // where the tube's centre sits across the stage
const FLOOR_Y = 0.925;        // where the machine stands
const GLASS_H = 0.125;        // how tall the tube is, fraction of the stage height
/* ONE machine for the whole showroom. It never moves: switching a mode changes what is on its
   screen, what plays behind it and the words on the left - the desk stays exactly where it is.
   screen = where the glass sits inside the file, as fractions of it (re-measure if replaced). */
/* Two layers of the same photograph, exactly on top of each other: the desk (with the computer
   painted out) and the computer alone. Only the computer reacts to the pointer. pcOrigin is the
   point it grows from - the middle of where it stands, in fractions of the file. */
const MACHINE = {
  desk: "machines/clip_desk.webp", pc: "machines/clip_pc.webp",
  screen: [.4257, .0510, .2283, .1970],   // the glass, measured on the DESK file
  pcBox: [.3233, .0095, .4616, .4265],    // where the computer sits in that same frame
};
let machineEl = null;
function machineBox() {
  const img = machineEl && machineEl.querySelector(".photo");
  if (!img || !img.naturalWidth) return null;
  const box = machineEl.getBoundingClientRect();
  const [sx, sy, sw, sh] = MACHINE.screen;
  const h = (box.height * GLASS_H) / sh;
  const w = img.naturalWidth * (h / img.naturalHeight);
  const top = box.height * FLOOR_Y - h;
  const left = box.width * GLASS_X - (sx + sw / 2) * w;
  return { left, top, width: w, height: h,
           glass: { left: left + sx * w, top: top + sy * h, width: sw * w, height: sh * h } };
}
function layoutMachine() {
  const r = machineBox();
  if (!r) return;
  const img = machineEl.querySelector(".photo");
  const pc = machineEl.querySelector(".pc");
  const pcimg = machineEl.querySelector(".pcimg");
  const g = machineEl.querySelector(".glass");
  const sh = machineEl.querySelector(".shadow");
  img.style.cssText = `left:${r.left}px;top:${r.top}px;width:${r.width}px;height:${r.height}px;`;
  // the computer is its own file, cropped to itself: only that rectangle listens to the pointer
  const [px, py, pw, ph] = MACHINE.pcBox;
  if (pcimg) pcimg.style.cssText = `left:${r.left + px * r.width}px;top:${r.top + py * r.height}px;` +
    `width:${pw * r.width}px;height:${ph * r.height}px;`;
  if (pc) pc.style.transformOrigin =
    `${r.left + (px + pw / 2) * r.width}px ${r.top + (py + ph) * r.height}px`;
  g.style.cssText = `left:${r.glass.left}px;top:${r.glass.top}px;width:${r.glass.width}px;height:${r.glass.height}px;`;
  const gw = r.width * 0.92;
  sh.style.cssText = `left:${r.left + (r.width - gw) / 2}px;top:${r.top + r.height - r.height * 0.10}px;` +
    `width:${gw}px;height:${Math.max(24, r.height * 0.22)}px;`;
  const sp = machineEl.querySelector(".spill");
  if (sp) {
    const gr = r.glass, w = gr.width * 3.4, h = gr.height * 3.2;
    sp.style.cssText = `left:${gr.left + gr.width / 2 - w / 2}px;top:${gr.top + gr.height * 0.1}px;` +
      `width:${w}px;height:${h}px;`;
  }
}
let blenderScene = null;
function buildMachine() {
  const wrap = el("div", "rig blender-rig"); machineEl = wrap;
  wrap.setAttribute("aria-busy", "true");
  wrap.appendChild(el("span", "blender-load", "Preparing your station"));
  wrap.querySelector('.blender-load').setAttribute('role', 'status');
  import(STATIC + "showroom3d/scene.js?v=review-3").then(async module => {
    if (!wrap.isConnected) return;
    const scene = await module.createShowroom(wrap, MODES[S.slide], () => {
      enterMachine(MODES[S.slide]);
    });
    if (!wrap.isConnected) { scene.dispose(); return; }
    blenderScene = scene;
    wrap.setAttribute("aria-busy", "false");
    scene.setMode(MODES[S.slide]);
  }).catch(error => {
    console.error("Blender showroom failed:", error);
    if (!wrap.isConnected) return;
    wrap.setAttribute("aria-busy", "false");
    wrap.classList.add("model-unavailable");
    const note = wrap.querySelector(".blender-load");
    if (note) note.textContent = "3D unavailable — use the station button to continue";
  });
  return wrap;
}
function buildPhotographicMachine() {
  const wrap = el("div", "rig"); machineEl = wrap;
  const shadow = el("div", "shadow");
  const desk = el("img", "photo"); desk.alt = ""; desk.decoding = "async";
  const pc = el("div", "pc");
  const pcimg = el("img", "pcimg"); pcimg.alt = ""; pcimg.decoding = "async";
  // the light the tube throws back into the room, on the desk in front of it
  const spill = el("div", "spill");
  const glass = el("div", "glass", '<span class="bulge"></span><span class="field-slot"></span>');
  glass.setAttribute("role", "button"); glass.tabIndex = 0;
  // a click on the machine sits you down at whatever mode is on its screen. A real drag is
  // measured, not flagged: the click event fires before the drag flag is cleared, so a pointer
  // that wandered a few pixels while pressing must still count as a click.
  const sit = () => { if (dragDist < 12) enterMachine(MODES[S.slide]); };
  const lift = (on) => pc.classList.toggle("lift", on);
  [pcimg, glass].forEach(n => {
    n.addEventListener("click", sit);
    n.addEventListener("mouseenter", () => lift(true));
    n.addEventListener("mouseleave", () => lift(false));
  });
  glass.addEventListener("keydown", e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); sit(); } });
  glass.addEventListener("focus", () => lift(true));
  glass.addEventListener("blur", () => lift(false));
  desk.addEventListener("click", sit);
  desk.addEventListener("load", () => { wrap.classList.add("has-photo"); layoutMachine(); });
  pcimg.addEventListener("load", () => layoutMachine());
  desk.src = STATIC + MACHINE.desk;
  pcimg.src = STATIC + MACHINE.pc;
  // only if the photograph cannot be loaded at all does the drawn machine stand in - building it
  // up front made it flash on screen for a frame on every repaint
  desk.addEventListener("error", () => {
    if (wrap.querySelector(".machine")) return;
    const drawn = el("div", "machine");
    drawn.innerHTML =
      `<div class="crt"><div class="screen"><span class="field-slot"></span><span class="glow"></span></div>` +
      `<span class="plate"><i>${STRIPES}</i>SHORTSLAB</span><span class="led"></span></div>` +
      `<div class="base"><span class="keys"></span><span class="pad"></span><span class="lamp"></span></div>`;
    drawn.addEventListener("click", sit);
    wrap.insertBefore(drawn, pc);
    paintScreen(MODES[S.slide], true);
  });
  pc.appendChild(pcimg); pc.appendChild(glass);
  // paint order IS the depth here: the desk, then the light it catches, then the machine
  wrap.appendChild(shadow); wrap.appendChild(desk); wrap.appendChild(spill); wrap.appendChild(pc);
  return wrap;
}
/* the screen changes channel: the old page rolls off, the new one is typed on */
function paintScreen(m, initial) {
  if (blenderScene) { blenderScene.setMode(m, {animate:!initial,direction:lastDir}); return; }
  if (!machineEl) return;
  const slots = machineEl.querySelectorAll(".field-slot");
  const glass = machineEl.querySelector(".glass");
  if (!slots.length) return;
  slots.forEach(slot => { slot.innerHTML = screenField(m); });
  glass.setAttribute("aria-label", "Sit down at the " + m.name);
  if (initial || REDUCED) return;
  glass.classList.remove("tune"); void glass.offsetWidth; glass.classList.add("tune");
  // the desk stands still, but it feels the scroll: a hard mechanical judder against the direction
  machineEl.style.setProperty("--dir", String(lastDir || 1));
  machineEl.classList.remove("jolt"); void machineEl.offsetWidth; machineEl.classList.add("jolt");
}
let lastDir = 1;
function renderRetroShowroom(main) {
  const room = el("div", "showroom"); showroomEl = room;
  const stage = el("div", "stage");
  const track = el("div", "track"); trackEl = track;
  MODES.forEach((m, i) => {
    const s = el("section", "setup"); s.dataset.mode = m.id; s.dataset.i = i;
    s.setAttribute("aria-label", m.name); s.setAttribute("aria-hidden", i === S.slide ? "false" : "true");
    s.appendChild(el("div", "haze"));
    // the copy travels with the slide: scrolling a mode really moves the words past the machine
    const hl = el("div", "headline");
    hl.innerHTML = `<div class="station-caption"><span>${String(i + 1).padStart(2, "0")} / ${String(MODES.length).padStart(2, "0")}</span><span>${esc(m.discipline)}</span></div><h1>${m.title.map(t => `<span>${esc(t)}</span>`).join("")}</h1><p>${esc(m.quiet)}</p><dl class="station-spec"><div><dt>Input</dt><dd>${esc(m.input)}</dd></div><div><dt>Output</dt><dd>${esc(m.output)}</dd></div></dl>`;
    const sit = el("a", "sit", `Open ${esc(m.name)} <span aria-hidden="true">↗</span>`); sit.href = "#";
    sit.addEventListener("click", e => { e.preventDefault(); enterMachine(m); });
    hl.appendChild(sit);
    s.appendChild(hl);
    track.appendChild(s);
  });
  stage.appendChild(buildMachine());
  stage.insertBefore(track, stage.firstChild);
  room.appendChild(stage);
  const deck = el("div", "deck");
  const prev = el("button", "arrow prev", '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 19V5M6 11l6-6 6 6"/></svg>');
  prev.type = "button"; prev.setAttribute("aria-label", "Machine above"); prev.addEventListener("click", () => goSlide(S.slide - 1));
  const next = el("button", "arrow next", '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 5v14M6 13l6 6 6-6"/></svg>');
  next.type = "button"; next.setAttribute("aria-label", "Machine below"); next.addEventListener("click", () => goSlide(S.slide + 1));
  const dots = el("div", "dots"); dots.id = "dots"; dots.setAttribute("role", "tablist"); dots.setAttribute("aria-label", "Machines");
  MODES.forEach((m, i) => {
    const d = el("button"); d.type = "button"; d.setAttribute("role", "tab"); d.setAttribute("aria-label", m.name);
    d.dataset.label = m.name;
    d.addEventListener("click", () => goSlide(i)); dots.appendChild(d);
  });
  deck.appendChild(prev); deck.appendChild(dots); deck.appendChild(next);
  room.appendChild(deck);
  const wayfinding = el("div", "showroom-wayfinding");
  wayfinding.innerHTML = '<span class="showroom-scroll-hint">Scroll to explore <span aria-hidden="true">↓</span></span>';
  const nextStation = el("button", "showroom-next-station");
  nextStation.type = "button";
  nextStation.addEventListener("click", () => goSlide(S.slide + 1));
  wayfinding.appendChild(nextStation);
  room.appendChild(wayfinding);
  if (pendingDraft) room.appendChild(couponNote());
  main.appendChild(room);
  bindShowroomGestures(room, track);
  paintSlide(true);
  if (!glassWatch) { glassWatch = true; window.addEventListener("resize", () => layoutMachine()); }
}
let glassWatch = false;   // the resize listener is bound once, for the life of the page
function paintSlide(initial) {
  if (!trackEl) return;
  S.slide = Math.min(MODES.length - 1, Math.max(0, +S.slide || 0));
  const m = MODES[S.slide];
  trackEl.style.transform = `translateY(${-S.slide * 100}%)`;
  [...trackEl.children].forEach((s, i) => {
    s.setAttribute("aria-hidden", i === S.slide ? "false" : "true");
    s.inert = i !== S.slide;
    s.classList.toggle("here", i === S.slide);
    // the words lag behind the scroll a little, so the move is felt and not just seen
    s.style.setProperty("--par", `${(i - S.slide) * -46}px`);
  });
  paintScreen(m, initial);
  layoutMachine();
  const dots = $("dots");
  if (dots) [...dots.children].forEach((d, i) => {
    d.setAttribute("aria-current", i === S.slide ? "true" : "false");
    d.setAttribute("aria-selected", i === S.slide ? "true" : "false");
  });
  // the ends of the line are visible: the arrow that leads nowhere is dead
  const up = document.querySelector(".deck .arrow.prev"), down = document.querySelector(".deck .arrow.next");
  if (up) up.disabled = S.slide === 0;
  if (down) down.disabled = S.slide === MODES.length - 1;
  const nextStation = document.querySelector(".showroom-next-station");
  if (nextStation) {
    const upcoming = MODES[S.slide + 1];
    nextStation.hidden = !upcoming;
    nextStation.textContent = upcoming ? `Next — ${upcoming.name} ↓` : "";
  }
  const scrollHint = document.querySelector(".showroom-scroll-hint");
  if (scrollHint) scrollHint.innerHTML = S.slide === MODES.length - 1
    ? 'Scroll up to explore <span aria-hidden="true">↑</span>'
    : 'Scroll to explore <span aria-hidden="true">↓</span>';
  document.querySelectorAll(".v3-top .tab").forEach((t, i) => {
    t.setAttribute("aria-selected", i === S.slide ? "true" : "false");
    t.classList.toggle("studio-nav-active", i === S.slide);
  });
  setBackdrop(m);
  // only the picture on the wall drifts with the scroll. The lamp and the haze belong to the room
  // and stay where they are - the light does not travel with the machines.
  const bg = $("v3-bg");
  if (bg) bg.style.setProperty("--bg-par", `${-S.slide * 3.4}%`);
}
function goSlide(i) {
  if (blenderScene?.isEntering) return;
  // the line of machines has a first and a last one: it stops there, it does not wrap around
  const next = Math.max(0, Math.min(MODES.length - 1, i));
  if (next === S.slide) { paintSlide(true); return; }
  lastDir = next > S.slide ? 1 : -1;
  S.slide = next;
  persist(); paintSlide(false);
}
let dragMoved = false, dragDist = 0;
function createWheelGesture() {
  let accumulated = 0, lastTime = -Infinity, direction = 0, consumed = false;
  return (delta, now) => {
    if (!Number.isFinite(delta) || Math.abs(delta) < 1) return 0;
    const sign = Math.sign(delta);
    const deliberateReverse = sign !== direction && Math.abs(delta) >= 16;
    if (now - lastTime > 180 || deliberateReverse) {
      accumulated = 0; consumed = false; direction = sign;
    }
    lastTime = now;
    if (consumed || sign !== direction) return 0;
    accumulated += delta;
    if (Math.abs(accumulated) < 70) return 0;
    consumed = true;
    return sign;
  };
}
function bindShowroomGestures(room, track) {
  let y0 = 0, dy = 0, t0 = 0, dragging = false, captured = false, h = 1;
  room.addEventListener("pointerdown", e => {
    if (e.button !== 0 || e.target.closest("button, a, .coupon")) return;
    dragging = true; dragMoved = false; dragDist = 0; y0 = e.clientY; dy = 0; t0 = performance.now(); h = room.clientHeight || 1;
    track.classList.add("dragging");
  });
  room.addEventListener("pointermove", e => {
    if (!dragging) return;
    dy = e.clientY - y0; dragDist = Math.abs(dy); if (dragDist > 6) dragMoved = true;
    // capture only once this really is a drag: a captured pointer retargets the click to the
    // showroom, and the machine underneath would never hear it
    if (dragMoved && !captured) { captured = true; try { room.setPointerCapture(e.pointerId); } catch (err) {} }
    track.style.transform = `translateY(calc(${-S.slide * 100}% + ${dy}px))`;
  });
  const end = (e) => {
    if (!dragging) return;
    dragging = false; track.classList.remove("dragging");
    if (captured) { captured = false; try { room.releasePointerCapture(e.pointerId); } catch (err) {} }
    const v = dy / Math.max(1, performance.now() - t0);
    if (dy < -h * 0.12 || v < -0.5) goSlide(S.slide + 1);
    else if (dy > h * 0.12 || v > 0.5) goSlide(S.slide - 1);
    else paintSlide(true);
    setTimeout(() => { dragMoved = false; }, 0);
  };
  room.addEventListener("pointerup", end); room.addEventListener("pointercancel", end);
  const wheelGesture = createWheelGesture();
  room.addEventListener("wheel", e => {
    // Pinch-to-zoom and horizontal gestures keep their browser behavior.
    if (e.ctrlKey || Math.abs(e.deltaX) > Math.abs(e.deltaY)) return;
    e.preventDefault();
    const units = e.deltaMode === 1 ? 16 : e.deltaMode === 2 ? room.clientHeight : 1;
    const step = wheelGesture(e.deltaY * units, performance.now());
    if (step) goSlide(S.slide + step);
  }, { passive: false });
}
document.addEventListener("keydown", e => {
  if (e.defaultPrevented || e.ctrlKey || e.metaKey || e.altKey) return;
  const tgt = e.target && typeof e.target.closest === "function" ? e.target : document.body;
  const inField = /^(INPUT|TEXTAREA|SELECT)$/.test(tgt.tagName || "") || tgt.isContentEditable;
  if (S.view === "showroom") {
    if (document.querySelector(".studio-home")) return;
    if (inField || tgt.closest(".scrim, .coupon")) return;
    if (e.key === "ArrowDown" || e.key === "ArrowRight") { e.preventDefault(); goSlide(S.slide + 1); }
    else if (e.key === "ArrowUp" || e.key === "ArrowLeft") { e.preventDefault(); goSlide(S.slide - 1); }
    else if ((e.key === "Enter" || e.code === "Enter" || e.code === "NumpadEnter") && !tgt.closest("button, a, .machine")) { e.preventDefault(); enterMachine(MODES[S.slide]); }
  } else if ((S.view === "home" || S.view === "wait" || S.view === "assets") && e.key === "Escape" && !document.querySelector(".scrim")
             && (!inField || (S.view === "assets" && tgt.type === "search"))) {      // the Library focuses its search box; Esc must still leave
    if (S.view === "wait" && ["running", "cancelling", "awaiting_approval"].includes(S.jobStatus)) return;  // never leave a live run by accident
    e.preventDefault(); goShowroom();
  }
});

/* ---- the backdrop: what this machine makes, as weather behind the stage */
let bgLayers = [], bgActive = -1, bgCurrent = "";
function ensureLayers() {
  if (bgLayers.length) return;
  const bg = el("div"); bg.id = "v3-bg";
  for (let i = 0; i < 2; i++) { const l = el("div", "bg-layer"); bg.appendChild(l); bgLayers.push(l); }
  document.body.insertBefore(bg, document.body.firstChild);
  const fx = el("div"); fx.id = "v3-fx"; fx.setAttribute("aria-hidden", "true"); document.body.appendChild(fx);
  const glass = el("div"); glass.id = "v3-glass"; glass.setAttribute("aria-hidden", "true"); document.body.appendChild(glass);
}
function setBackdrop(m) {
  ensureLayers();
  // The Blender wall owns the preview. Do not decode a hidden second video.
  if (machineEl?.classList.contains("blender-rig")) m = null;
  const key = m ? m.id : "";
  if (key === bgCurrent) return;
  bgCurrent = key;
  bgLayers.forEach(l => { l.classList.remove("on"); const v = l.querySelector("video"); if (v) { try { v.pause(); } catch (e) {} } });
  if (!m) return;
  bgActive = (bgActive + 1) % 2;
  const layer = bgLayers[bgActive];
  layer.innerHTML = "";
  // The mode's own footage IS the room: it fills the whole background, and the machine stands in
  // front of it. Yes, a 9:16 Short loses its sides on a wide window - that is the trade the owner
  // asked for, because a contained panel read as a postage stamp.
  if (m.poster) {
    const im = el("img", "wash"); im.alt = ""; im.decoding = "async"; im.src = STATIC + m.poster;
    layer.appendChild(im);
  }
  if (m.video && !REDUCED) {
    // poster first; the video only fades in once it has decoded, and it runs slow
    const v = el("video", "wash"); v.muted = true; v.loop = true; v.playsInline = true; v.preload = "auto"; v.setAttribute("aria-hidden", "true");
    v.addEventListener("canplay", () => {
      if (bgCurrent !== key) return;
      v.playbackRate = 0.6; v.play().then(() => v.classList.add("ready")).catch(() => {});
    }, { once: true });
    v.src = STATIC + m.video;
    layer.appendChild(v);
  }
  requestAnimationFrame(() => { if (bgCurrent === key) layer.classList.add("on"); });
}

/* ---- the zoom into the screen: the image expands, the glass bulges, the field goes blue */
/* A flight is in progress until this moment passes. A timestamp, not a flag: an animation whose
   finish event never arrives (a cancelled zoom, a re-render mid-flight) can then never leave the
   showroom permanently dead - which is exactly what a stuck boolean did. */
let zoomUntil = 0;
const flying = () => performance.now() < zoomUntil;
function enterMachine(m) {
  if (flying()) return;
  const land = () => {
    if (m.page === "v3") {
      S.mode = m.mode; S.view = "home"; persist(); render();
      if (!REDUCED) {
        const main = $("v3-main");
        main.classList.add("cinematic-arrival");
        setTimeout(() => main.classList.remove("cinematic-arrival"), 850);
      }
      return;
    }
    // this machine runs in the classic console: the screen lands, then hands over
    window.location.href = m.page;
  };
  if (blenderScene) { blenderScene.enter(land); return; }
  const glass = machineEl && machineEl.querySelector(".glass");
  const stage = $("v3-main");
  if (REDUCED || !glass || !stage || !machineEl.classList.contains("has-photo")) { land(); return; }
  const vw = window.innerWidth, vh = window.innerHeight;
  const r = glass.getBoundingClientRect();
  const cx = r.left + r.width / 2, cy = r.top + r.height / 2;

  /* The camera pushes INTO the tube. Nothing is morphed into the shape of the window any more -
     that is what looked wrong: a small rectangle stretched over the whole screen. The whole room
     is scaled around the centre of the glass instead, so the desk, the words and the wall all
     grow past the edges the way they would if you walked into the picture, and the glass ends up
     filling the frame at its own proportions. What is written on it fades out in the first third,
     because letters blown up nine times only ever arrive as half words. */
  const k = Math.max(vw / r.width, vh / r.height) * 1.04;
  /* The anchor is the middle of the TUBE'S SCREEN: the room grows out of the glass, so the point
     you are flying at never drifts, and the glass is carried into the middle of the window while
     it grows. (cx, cy) is the centre of the glass rectangle, measured, not guessed. */
  const tx = vw / 2 - cx, ty = vh / 2 - cy;
  const dur = 820;
  zoomUntil = performance.now() + dur + 320;
  stage.style.transformOrigin = `${cx}px ${cy}px`;
  stage.style.willChange = "transform";
  const at = (p, e) => `translate(${tx * e}px, ${ty * e}px) scale(${1 + (k - 1) * p})`;
  const push = stage.animate(
    [{ transform: at(0, 0) }, { transform: at(0.26, 0.45), offset: 0.45 }, { transform: at(1, 1) }],
    { duration: dur, easing: "cubic-bezier(.5,0,.22,1)", fill: "forwards" });
  const field = machineEl.querySelector(".field");
  if (field) field.animate([{ opacity: 1 }, { opacity: 0 }], { duration: dur * 0.34, easing: "ease-out", fill: "forwards" });

  // the blue arrives over the last third, so the run page is never seen being built
  const fill = el("div", "zoom-fill");
  document.body.appendChild(fill);
  fill.animate([{ opacity: 0, offset: 0 }, { opacity: 0, offset: 0.55 }, { opacity: 1, offset: 0.94 }, { opacity: 1, offset: 1 }],
    { duration: dur, fill: "forwards" });

  /* Every step of the landing is also on a timer. An animation only advances while the page is
     visible: on a hidden tab the finish event never arrives, and the flight would strand the user
     on a blue pane with the run page underneath it. The timers are the same lengths, so nothing
     looks different when the page IS visible. */
  let landed = false;
  const arrive = () => {
    if (landed) return;
    landed = true;
    push.cancel();
    stage.style.transform = ""; stage.style.transformOrigin = ""; stage.style.willChange = "";
    // whatever the backdrop was doing, it is not allowed to dim the page you landed on
    const bg = $("v3-bg");
    if (bg) { bg.getAnimations().forEach(x => x.cancel()); bg.style.opacity = ""; }
    land();
    zoomUntil = 0;
    if (m.page !== "v3") return;
    fill.animate([{ opacity: 1 }, { opacity: 0 }], { duration: 300, easing: "ease-out", fill: "forwards" })
      .addEventListener("finish", () => fill.remove());
    setTimeout(() => fill.remove(), 700);
  };
  push.addEventListener("finish", arrive);
  setTimeout(arrive, dur + 260);
}
function zoomOut(done) {
  if (REDUCED || flying()) { done(); return; }
  const app = $("v3");
  const a = app.animate([
    { transform: "scale(1)", opacity: 1, filter: "none" },
    { transform: "scale(.82)", opacity: 0, filter: "brightness(1.8) blur(4px)" },
  ], { duration: 320, easing: "cubic-bezier(.6,0,.8,.4)", fill: "forwards" });
  zoomUntil = performance.now() + 320 + 300;
  let out = false;
  const back = () => {
    if (out) return;
    out = true;
    a.cancel();
    done();
    zoomUntil = 0;
    app.animate([{ opacity: 0, transform: "scale(1.04)" }, { opacity: 1, transform: "scale(1)" }], { duration: 380, easing: "cubic-bezier(.2,.8,.2,1)" });
  };
  a.addEventListener("finish", back);
  setTimeout(back, 620);
}

/* ---- the boot screen: once per browser session, skipped by any key or click */
/* The boot page. Once per browser session on the way in, and again on the way out when a machine
   is switched off from the brand: the system comes up, the system goes down. `opts.force` plays it
   regardless of the session flag; `opts.flash` skips the loading bar and shows only the tail - the
   blue field for a breath, then the tube collapsing. */
/* Opening the timeline editor is a full page load, and building its library takes seconds. Until
   now the window just sat there: the click did nothing visible, and people clicked again. This is
   the machine loading a program - the boot field without the boot, with the program's name and a
   bar that runs until the new document takes over. It is never dismissed by a timer, because the
   thing that ends it is the navigation itself. */
function loadWindow(href, program) {
  if (REDUCED) { location.href = href; return; }
  document.body.classList.add("is-boot");
  const b = el("div", "boot loading");
  b.setAttribute("role", "status");
  b.setAttribute("aria-label", "Loading " + program);
  b.innerHTML = `<div class="boot-field"><div class="boot-mark">` +
    `<div class="boot-logo"><span class="stripes">${STRIPES}</span>SHORTSLAB</div>` +
    `<div class="boot-line"><b>${esc(program)}</b><b>loading</b></div></div>` +
    `<div class="boot-bar indet" aria-hidden="true"><i></i></div></div>`;
  document.body.appendChild(b);
  // paint the overlay BEFORE handing the tab over, or the browser goes straight to a white frame
  requestAnimationFrame(() => setTimeout(() => { location.href = href; }, 30));
}
function timelineLink(node, slug, program) {
  node.href = "/timeline?slug=" + encodeURIComponent(slug);
  node.addEventListener("click", e => {
    if (e.ctrlKey || e.metaKey || e.shiftKey || e.button) return;   // let a new tab be a new tab
    e.preventDefault();
    loadWindow(node.href, program || "Timeline Editor");
  });
  return node;
}
function splash(done, opts) {
  const o = opts || {};
  let seen = false;
  try { seen = sessionStorage.getItem(BOOTED_KEY) === "1"; } catch (e) {}
  if ((seen && !o.force) || REDUCED) { done(); return; }
  try { sessionStorage.setItem(BOOTED_KEY, "1"); } catch (e) {}
  document.body.classList.add("is-boot");
  const b = el("div", "boot"); b.setAttribute("role", "status"); b.setAttribute("aria-label", "Starting");
  b.innerHTML = `<div class="boot-field"><div class="boot-mark"><div class="boot-logo"><span class="stripes">${STRIPES}</span>SHORTSLAB</div>` +
    `<div class="boot-line"><b>Shortslab Studio System</b><b>Version 3.0</b></div></div>` +
    `<div class="boot-bar" aria-hidden="true"><i></i></div><span></span>` +
    `<div class="boot-copy">Copyright (c) Shortslab, 2026. All Rights Reserved.</div></div><span class="skip">any key to skip</span>`;
  if (o.flash) b.classList.add("flash");     // switching off is not a boot: no wordmark, no copyright
  document.body.appendChild(b);
  const bar = b.querySelector(".boot-bar > i");
  let step = 0, finished = false;
  const steps = o.steps || 14;
  let tick = 0;
  if (o.flash) {
    bar.style.setProperty("--fill", "100%");
    setTimeout(off, o.flash);
  } else {
    tick = setInterval(() => { step++; bar.style.setProperty("--fill", Math.min(100, step / steps * 100) + "%"); if (step >= steps) { clearInterval(tick); setTimeout(off, 260); } }, o.pace || 95);
  }
  function off() {
    if (finished) return; finished = true; clearInterval(tick);
    document.removeEventListener("keydown", off); b.removeEventListener("click", off);
    b.classList.add("off");
    setTimeout(() => { b.remove(); document.body.classList.remove("is-boot"); done(); }, 440);
  }
  document.addEventListener("keydown", off); b.addEventListener("click", off);
}

/* ================================================================== the other windows
   Every screen the app has is a program this machine runs. What follows are the ones that used
   to open in the older chat console: the Library, the Sketch Station (longform), the Physics
   Bench, the Story Station (reddit) and the Action Edit on the
   Effects Station. Each posts EXACTLY what the older console posted - same endpoint, same field
   names, same values (static/chat-shell.js is the reference; the tests compare the two). */

/* ---- which route a screen answers to, so a reload lands on the same program */
function routeFor(mode) {
  return { challenge: "/challenge", longform: "/longform", physics: "/?flow=physics", reddit: "/reddit",
           enhance: S.enhance && S.enhance.kind === "actionedit" ? "/actionedit" : "/" + ({ sfx: "sfx", visual: "visual", captions: "captions", asmr: "?flow=asmr" }[S.enhance ? S.enhance.kind : ""] || "") }[mode] || "/";
}
/* a page: the head, then the two columns the run page uses */
function pageHead(title, quiet, cap) {
  const head = el("div", "page-head");
  head.appendChild(el("h1", "h1", esc(title)));
  if (quiet) head.appendChild(el("span", "muted", esc(quiet)));
  if (cap) head.appendChild(el("span", "cap", esc(cap)));
  return head;
}
function panel(title, ...nodes) {
  const p = el("section", "panel");
  const head = el("div", "panel-head"); head.appendChild(el("span", "label", esc(title))); p.appendChild(head);
  nodes.filter(Boolean).forEach(n => p.appendChild(n));
  return p;
}
/* the status line at the foot: what will be sent, and the one command that sends it */
function dockWith(segments, go, note) {
  const dock = $("v3-dock"); dock.hidden = false; dock.innerHTML = "";
  const sum = el("div", "dock-sum");
  segments.filter(Boolean).forEach(([k, v]) => { sum.innerHTML += `<span class="sum-seg"><small>${esc(k)}</small><b>${esc(v)}</b></span>`; });
  dock.appendChild(sum);
  const n = el("span", "dock-note", esc(note || "")); n.hidden = !note; dock.appendChild(n);
  n.id = "dock-why";
  if (go) {
    go.id = "go"; go.classList.add("primary");
    // A NOTE IS ALWAYS A BLOCKER. Every caller passes the reason the run cannot start -
    // "Attach a video first.", "Pick a simulation or describe one." - and passes "" when it
    // can. The button was full-strength blue and pointer-cursored the whole time, so it read
    // as armed, and a screen reader was told nothing at all: the reason sat in a separate
    // span at the other end of the dock with no relationship to the control it was about.
    const blocked = !!note;
    if (go.tagName === "BUTTON") go.disabled = blocked;
    go.setAttribute("aria-disabled", blocked ? "true" : "false");
    if (blocked) { go.setAttribute("aria-describedby", n.id); go.title = note; }
    else { go.removeAttribute("aria-describedby"); go.removeAttribute("title"); }
    dock.appendChild(go);
  }
}
function afterJobStart(r) {
  const jid = new URL(r.url, location.href).searchParams.get("id");
  if (!jid) throw new Error("no job id");
  return jid;
}

/* ------------------------------------------------------------------ the Library (/assets) */
let ASSETS = { rows: [], hidden_count: 0, loaded: false, visible: 48 };
function goAssets(q) {
  stopPolling();
  S.view = "assets";
  if (q != null) S.assets.q = q;
  history.replaceState(null, "", "/assets" + (S.assets.q ? "?q=" + encodeURIComponent(S.assets.q) : ""));
  persist(); render();
}
async function renderAssets(main) {
  const page = el("div", "page assets");
  page.appendChild(pageHead("Library", "Your ideas, in motion. All your projects in one place.", "Open the cut, load the settings, or hide what is done."));
  const bar = el("div", "lib-bar");
  const search = el("input"); search.type = "search"; search.placeholder = "Search projects"; search.value = S.assets.q || "";
  search.setAttribute("aria-label", "Search projects"); search.autocomplete = "off";
  bar.appendChild(search);
  const count = el("span", "cap"); count.id = "lib-count"; bar.appendChild(count);
  const hid = btn(S.assets.hidden ? "Back to the library" : "Hidden projects", () => { S.assets.hidden = !S.assets.hidden; ASSETS.loaded = false; persist(); render(); }, "sm ghost");
  hid.id = "lib-hidden"; bar.appendChild(hid);
  page.appendChild(bar);
  let libraryFilter = "all";
  const filters = el("div", "studio-library-filters"); filters.setAttribute("role", "group"); filters.setAttribute("aria-label", "Filter projects");
  [["all", "All projects"], ["ready", "With video"], ["editing", "In edit"], ["attention", "Needs attention"]].forEach(([key, label]) => {
    const button = btn(label, () => {
      libraryFilter = key; ASSETS.visible = 48;
      filters.querySelectorAll("button").forEach(b => b.setAttribute("aria-pressed", String(b === button)));
      paint();
    }, "ghost sm");
    button.setAttribute("aria-pressed", String(key === "all")); filters.appendChild(button);
  });
  page.appendChild(filters);
  const grid = el("div", "lib-grid"); grid.id = "lib-grid";
  grid.appendChild(el("div", "cap", "Loading your projects…"));
  page.appendChild(grid);
  const foot = el("div", "lib-foot"); page.appendChild(foot);
  main.appendChild(page);
  const paint = () => {
    const q = (search.value || "").trim().toLowerCase();
    S.assets.q = search.value || "";
    const rows = ASSETS.rows.filter(p => {
      const matchesText = !q || (p.title || "").toLowerCase().includes(q) || (p.slug || "").toLowerCase().includes(q);
      const matchesFilter = libraryFilter === "all" || (libraryFilter === "ready" && !!p.video_url)
        || (libraryFilter === "editing" && p.has_timeline && !p.video_url && !p.failed)
        || (libraryFilter === "attention" && (p.failed || p.awaiting));
      return matchesText && matchesFilter;
    });
    grid.innerHTML = "";
    rows.slice(0, ASSETS.visible).forEach(p => grid.appendChild(libraryCard(p)));
    if (!rows.length) grid.appendChild(el("div", "cap lib-empty", ASSETS.rows.length ? "Nothing on the shelf matches that." : (S.assets.hidden ? "Nothing is hidden." : "Your first story starts in the studio. Create a Short to see it here.")));
    // THREE STATEMENTS ABOUT ONE LIST, ALL ON SCREEN AT ONCE: this read "235 of 235" while 48
    // cards were drawn and a button underneath offered to "Show 48 more". It counted what was
    // FETCHED against what MATCHED, and the reader can only see what was drawn.
    const shown = Math.min(rows.length, ASSETS.visible);
    const filtered = rows.length !== ASSETS.rows.length;
    count.textContent =
      (shown < rows.length ? `${shown} of ${rows.length}${filtered ? " matching" : ""}`
       : filtered ? `${rows.length} of ${ASSETS.rows.length}`
       : `${ASSETS.rows.length} project${ASSETS.rows.length === 1 ? "" : "s"}`)
      + (S.assets.hidden ? " · hidden" : "")
      + (!S.assets.hidden && ASSETS.hidden_count ? ` · ${ASSETS.hidden_count} hidden` : "");
    foot.innerHTML = "";
    if (rows.length > ASSETS.visible) foot.appendChild(btn(`Show ${Math.min(48, rows.length - ASSETS.visible)} more`, () => { ASSETS.visible += 48; paint(); }, "sm"));
  };
  search.addEventListener("input", () => { ASSETS.visible = 48; paint(); persist(); });
  if (!ASSETS.loaded || ASSETS.hidden !== !!S.assets.hidden) {
    try {
      const d = await jget("/projects-list" + (S.assets.hidden ? "?hidden=1" : ""));
      ASSETS = { rows: d.projects || [], hidden_count: d.hidden_count || 0, loaded: true, visible: 48, hidden: !!S.assets.hidden };
    } catch (e) { grid.innerHTML = ""; grid.appendChild(el("div", "cap", "The shelf could not be read: " + esc(e))); return; }
  }
  if (S.view !== "assets") return;
  paint();
  setTimeout(() => { try { search.focus({ preventScroll: true }); $("v3-main").scrollTop = 0; } catch (e) {} }, 30);
}
// "2026-09-08 14:33" against a list sorted by recency answers a question nobody asked. The
// same field is printed as "09/08 02:29" on the Start screen, so the app had two formats for
// one value and neither of them was the one a recency-sorted shelf needs.
function whenText(stamp) {
  const t = Date.parse(String(stamp || "").replace(" ", "T"));
  if (!stamp || isNaN(t)) return String(stamp || "");
  const mins = Math.round((Date.now() - t) / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return mins + (mins === 1 ? " min ago" : " mins ago");
  const hours = Math.round(mins / 60);
  if (hours < 24) return hours + (hours === 1 ? " hour ago" : " hours ago");
  const days = Math.round(hours / 24);
  if (days === 1) return "yesterday";
  if (days < 7) return days + " days ago";
  return String(stamp).slice(0, 10);
}
function projectState(p) {
  if (p.running) return ["running", "live"];
  if (p.awaiting) return ["needs you", "warn"];
  if (p.failed) return ["failed", "fail"];
  if (p.longform) {
    // A Sketch project reports its own status, and every one of them was handed an empty class -
    // so "Done", "Voiceover ready" and "Voiceover in progress" arrived as the same cream word in
    // the same chip. The one state that means "come back later" looked exactly like "finished".
    const s = String(p.status || "sketch");
    if (/in progress|rendering|working/i.test(s)) return [s, "live"];
    if (/needs|missing|error|failed/i.test(s)) return [s, "warn"];
    return [s, ""];             // "Done" is the resting state; neutral is what it should read as
  }
  if (p.has_timeline && !p.has_video) return ["in edit", "warn"];
  return ["", ""];
}
function libraryCard(p) {
  const c = el("article", "lib-card");
  c.setAttribute("aria-label", p.title || p.slug);
  const fig = el("div", "lib-fig");
  const [st, cls] = projectState(p);
  if (p.thumb_url) { const im = el("img"); im.src = p.thumb_url; im.alt = ""; im.loading = "lazy"; im.decoding = "async"; fig.appendChild(im); }
  else {
    // A CARD MUST NOT CONTRADICT ITSELF. Any project without a poster said "Work in progress" -
    // including the 25 of 48 whose badge, two centimetres above it, said FAILED. The placeholder
    // says what the state says; only a project that really is unfinished says so.
    const placeholder = el("div", "studio-project-placeholder");
    const caption = p.failed ? "No video was produced"
      : p.running ? "Working on it"
      : p.longform ? "Sketch project" : "Work in progress";
    placeholder.innerHTML = svgIcon("play") + "<span>" + esc(caption) + "</span>";
    fig.appendChild(placeholder);
  }
  if (st) fig.appendChild(el("span", "lib-state " + cls, esc(st)));
  if (p.kind && !p.longform) fig.appendChild(el("span", "lib-kind", esc(p.kind)));
  c.appendChild(fig);
  const body = el("div", "lib-body");
  const title = el("b", "", esc(p.title || p.slug)); title.title = p.slug; body.appendChild(title);
  // A count of zero is not information. Measured on the shelf: all 48 visible cards ended
  // their meta line in a zero - "0 images" on 40 of them, "0 frames" on 8 - because those
  // modes cannot produce that thing at all, so the number could never be anything else. A
  // counter earns its place by having something to report.
  const cnt = p.counters || {};
  const bits = [];
  if (p.longform) { if (p.images) bits.push(p.images + " frames"); }
  else {
    if (cnt.clips) bits.push(cnt.clips + " clips");
    if (cnt.gpt) bits.push(cnt.gpt + " images");
    if (cnt.web) bits.push(cnt.web + " web");
  }
  body.appendChild(el("span", "cap", esc([whenText(p.edited)].concat(bits).filter(Boolean).join(" · "))));
  c.appendChild(body);
  // ONE THING TO DO, and the housekeeping out of the way. This was six chips in a 3x2 block -
  // Timeline, Load, Video, Files, Rename, Hide - all the same size, the same weight and the same
  // colour, so every card asked the reader to choose between six things when five of them are
  // not why anyone opens a shelf. The card now leads with the single action it exists for, keeps
  // at most one other place worth going as a quiet second, and files the rest behind a menu.
  const openIt = () => (p.longform ? openLongformProject(p) : loadProject(p.slug, p));
  const openLabel = p.longform ? "Open" : "Load";
  const goTimeline = () => loadWindow("/timeline?slug=" + encodeURIComponent(p.slug), "Timeline Editor");
  const watch = () => window.open(p.video_url, "_blank", "noopener");

  const acts = el("div", "lib-acts");
  let primary, second;
  if (p.video_url) {
    primary = { label: "Watch", run: watch, href: p.video_url };
    second = p.has_timeline ? { label: "Timeline", run: goTimeline, href: "/timeline?slug=" + encodeURIComponent(p.slug) }
                            : { label: openLabel, run: openIt };
  } else if (p.has_timeline) {
    primary = { label: "Timeline", run: goTimeline, href: "/timeline?slug=" + encodeURIComponent(p.slug) };
    second = { label: openLabel, run: openIt };
  } else {
    primary = { label: openLabel, run: openIt };
    second = null;
  }

  // The poster is the biggest thing on the card and did nothing at all: it now does whatever the
  // primary button does, which is what a picture of a video is for.
  fig.classList.add("is-open");
  fig.setAttribute("role", "button");
  fig.setAttribute("tabindex", "0");
  fig.setAttribute("aria-label", primary.label + ": " + (p.title || p.slug));
  fig.addEventListener("click", primary.run);
  fig.addEventListener("keydown", e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); primary.run(); } });

  const mkAction = (spec, cls) => {
    if (spec.href) {
      const a = el("a", "btn sm " + cls, esc(spec.label));
      a.href = spec.href;
      if (spec.href === p.video_url) { a.target = "_blank"; a.rel = "noopener"; }
      else a.addEventListener("click", e => { if (e.ctrlKey || e.metaKey || e.shiftKey || e.button) return; e.preventDefault(); spec.run(); });
      return a;
    }
    return btn(spec.label, spec.run, "sm " + cls);
  };
  acts.appendChild(mkAction(primary, "lib-go"));
  if (second) acts.appendChild(mkAction(second, "ghost"));

  const rest = [];
  // Nothing may be lost on the way into the menu. A card that has BOTH a video and a timeline
  // spends its two visible slots on Watch and Timeline, and loading the project into the studio
  // would otherwise have no route at all - it goes to the top of the menu instead.
  const shown = [primary.label, second && second.label];
  if (shown.indexOf(openLabel) < 0) rest.push({ label: openLabel, run: openIt });
  if (p.results_url) rest.push({ label: "Files", href: p.results_url });
  rest.push({ label: "Rename", run: async () => {
    const next = prompt("Project title", p.title || p.slug);
    if (next == null || !next.trim() || next.trim() === p.title) return;
    const d = await jpost("/rename-project", { slug: p.slug, title: next.trim() });
    if (d && d.ok !== false) { p.title = next.trim(); title.textContent = p.title; toast("Renamed."); }
    else toast("Could not rename: " + ((d && d.error) || "unknown"), true);
  } });
  if (!p.longform) rest.push({ label: S.assets.hidden ? "Unhide" : "Hide", run: async () => {
    await jpost("/hide-project", { slug: p.slug, hidden: !S.assets.hidden });
    ASSETS.rows = ASSETS.rows.filter(x => x.slug !== p.slug); c.remove();
    const n = $("lib-count"); if (n) n.textContent = `${ASSETS.rows.length} projects`;
  } });

  const more = el("div", "lib-more");
  const trigger = btn("More", null, "sm ghost lib-more-btn");
  trigger.setAttribute("aria-haspopup", "menu");
  trigger.setAttribute("aria-expanded", "false");
  trigger.setAttribute("aria-label", "More actions for " + (p.title || p.slug));
  const sheet = el("div", "lib-menu");
  sheet.setAttribute("role", "menu");
  rest.forEach(item => {
    let node;
    if (item.href) { node = el("a", "lib-menu-item", esc(item.label)); node.href = item.href; }
    else { node = btn(item.label, () => { closeMenu(); item.run(); }, "lib-menu-item"); }
    node.setAttribute("role", "menuitem");
    sheet.appendChild(node);
  });
  function closeMenu() {
    more.classList.remove("open");
    trigger.setAttribute("aria-expanded", "false");
    document.removeEventListener("click", onAway, true);
    document.removeEventListener("keydown", onEsc, true);
  }
  function onAway(e) { if (!more.contains(e.target)) closeMenu(); }
  function onEsc(e) { if (e.key === "Escape") { closeMenu(); trigger.focus(); } }
  trigger.addEventListener("click", () => {
    if (more.classList.contains("open")) { closeMenu(); return; }
    document.querySelectorAll(".lib-more.open").forEach(o => o.classList.remove("open"));
    more.classList.add("open");
    trigger.setAttribute("aria-expanded", "true");
    document.addEventListener("click", onAway, true);
    document.addEventListener("keydown", onEsc, true);
  });
  more.appendChild(trigger); more.appendChild(sheet);
  acts.appendChild(more);
  c.appendChild(acts);
  return c;
}

/* ------------------------------------------------------------------ the Sketch Station (/longform)
   Paste the script, pick the canvas and the narrator; the machine does voiceover, timestamps,
   one doodle per timestamp and the assembled video. Posts to /longform-run with the fields the
   older console posted (chat-shell.js submitLongform), nothing added, nothing renamed. */
function longformDefaults() {
  return { ...SKETCH_DEFAULTS, script: "", aspect: "16:9", image_zoom: false, multilang: false, hook_intro: false, hook_text: "",
           halt_after_speech: true, reasoning_mode: "", tts_voice_instruction: "", tts_pitch: "0", tts_sample_rate: "24000", tts_output_format: "mp3",
           slug: "", title: "", voiceover_ready: false };
}
function applySketchDefaults(L) {
  Object.keys(SKETCH_DEFAULTS).forEach(k => { if (L[k] == null || L[k] === "") L[k] = SKETCH_DEFAULTS[k]; });
  return L;
}
function openLongformProject(info) {
  const L = S.longform;
  L.slug = info.slug; L.title = info.title || info.slug; L.script = info.script || ""; L.voiceover_ready = !!info.voiceover_ready;
  ["tts_model", "tts_voice", "reasoning_model", "reasoning_mode", "tts_voice_instruction", "tts_language", "tts_native_speed",
   "tts_volume", "tts_pitch", "tts_sample_rate", "tts_output_format"].forEach(k => { if (info[k] != null && info[k] !== "") L[k] = info[k]; });
  if (info.halt_after_speech != null) L.halt_after_speech = !!info.halt_after_speech;
  S.mode = "longform"; S.view = "home"; S.slide = Math.max(0, MODES.findIndex(m => m.id === "longform"));
  history.replaceState(null, "", "/longform"); persist(); render();
  toast("Opened " + L.title + (L.voiceover_ready ? " - the voiceover is already paid for; a new run reuses it." : ""));
}
function longformProblem() {
  return (S.longform.script || "").trim().length < 40 ? "The script needs at least a few sentences." : "";
}
function renderLongform(main) {
  const L = applySketchDefaults(S.longform);
  resetTtsVoice(L);
  const page = el("div", "page");
  if (pendingDraft) page.appendChild(restoreBanner());
  page.appendChild(pageHead("Sketch Station", "A stickman explainer, drawn frame by frame while your narration is told.",
    "Long 16:9 video, or a one-minute 9:16 short in the same style."));
  const grid = el("div", "run-grid");

  // the script
  const p = el("section", "panel");
  const head = el("div", "panel-head"); head.appendChild(el("span", "label", "Script"));
  const meter = el("span", "cap"); head.appendChild(meter); p.appendChild(head);
  if (L.slug) {
    const row = el("div", "fld row");
    row.appendChild(el("span", "lbl", esc("Opened: " + (L.title || L.slug))));
    const acts = el("div", "chips");
    acts.appendChild(btn("Speech parts", async () => {
      const d = await jpost("/longform-speech-review", { slug: L.slug });
      if (d && d.ok) { const jid = d.id || String(d.job || "").split("id=")[1]; if (jid) { startJob(jid, "longform"); return; } }
      toast((d && d.error) || "Could not open the speech parts.", true);
    }, "sm ghost"));
    acts.appendChild(btn("3 thumbnails + titles", async () => {
      const d = await jpost("/longform-thumbnail", { slug: L.slug });
      if (d && d.ok && d.id) { startJob(d.id, "longform"); return; }
      toast((d && d.error) || "Could not start the thumbnails.", true);
    }, "sm ghost"));
    const fe = el("a", "btn sm ghost", "Frame editor"); fe.href = "/longform?ui=chat"; fe.title = "The pre-render frame editor still runs in the classic console";
    acts.appendChild(fe);
    acts.appendChild(btn("Close", () => { S.longform = longformDefaults(); persist(); render(); }, "sm ghost"));
    row.appendChild(acts); p.appendChild(row);
  }
  const ta = el("textarea", "script"); ta.id = "script-box"; ta.setAttribute("aria-label", "Video script"); ta.placeholder = T.longform_script_ph || "Paste your full script here...";
  ta.value = L.script || "";
  ta.addEventListener("input", () => { L.script = ta.value; paintMeter(); persist(); });
  ta.addEventListener("change", () => { renderLongformDock(); });
  ta.addEventListener("keydown", e => { if ((e.ctrlKey || e.metaKey) && e.key === "Enter") { e.preventDefault(); submitLongform(); } });
  p.appendChild(ta);
  function paintMeter() {
    const words = wordCount(L.script);
    const mins = words / (L.aspect === "9:16" ? 160 : 145);
    meter.textContent = `${words} words · about ${mins < 1 ? Math.round(mins * 60) + " s" : roughMinutes(mins * 60)}` + (L.aspect === "9:16" ? " · a short wants 150-170 words" : "");
  }
  paintMeter();
  const tools = el("div", "script-tools");
  tools.appendChild(btn("Open an existing Sketch Explainer", openLongformPicker, "sm"));
  p.appendChild(tools);
  grid.appendChild(p);

  // the settings
  const groups = el("div", "groups");
  const fmt = [];
  fmt.push(selectField("Video format", [{ value: "16:9", label: "Long video - 16:9, full script" }, { value: "9:16", label: "Short - 9:16 vertical, ~1 minute script" }],
    L.aspect || "16:9", v => {
      const changed = (L.aspect || "16:9") !== v; L.aspect = v;
      // a short lives on energy: the upbeat delivery fills an EMPTY direction, never a typed one
      if (changed && v === "9:16" && !String(L.tts_voice_instruction || "").trim()) L.tts_voice_instruction = SHORTS_VOICE_INSTRUCTION;
      if (changed && v === "9:16" && String(L.tts_native_speed || "1") === "1") L.tts_native_speed = SHORTS_VOICE_SPEED;
      else if (changed && v === "16:9" && String(L.tts_native_speed || "") === SHORTS_VOICE_SPEED) L.tts_native_speed = "1";
      if (changed) setTimeout(render, 0);
    }));
  fmt.push(toggleField("Slow zoom on the images", L.image_zoom === true, v => L.image_zoom = v, "A 5% push-in across each image's hold - enough to stop the frame feeling frozen."));
  fmt.push(toggleField("Extra audio tracks in 7 languages", L.multilang === true, v => L.multilang = v,
    "Runs AFTER the video: seven translations and seven full voiceovers, one .mp3 per language for YouTube. The most expensive switch here."));
  if ((L.aspect || "16:9") === "9:16") {
    const sentences = String(L.script || "").split(/(?<=[.!?])\s+/).map(t => t.trim()).filter(t => t.length > 11).slice(0, 8);
    if (L.hook_text && !sentences.includes(L.hook_text)) L.hook_text = "";
    fmt.push(el("div", "subcap", "Google hook intro"));
    fmt.push(toggleField("Start with the typed Google search", L.hook_intro === true, v => { L.hook_intro = v; render(); },
      "A 4s clip of the hook being typed into Google, spoken by the same narrator, ending in a fast zoom into the query."));
    if (L.hook_intro === true) {
      const chips = el("div", "chips");
      sentences.forEach(t => {
        const b = btn((L.hook_text === t ? "* " : "") + t.slice(0, 64), () => { L.hook_text = L.hook_text === t ? "" : t; persist(); render(); }, "sm" + (L.hook_text === t ? " on" : ""));
        chips.appendChild(b);
      });
      if (!sentences.length) chips.appendChild(el("span", "cap", "Write the script first - the hook is one of its sentences."));
      fmt.push(chips);
      fmt.push(el("div", "hint", "Mark the sentence to type; unmarked, the first sentence is used."));
    }
  }
  groups.appendChild(group("Format", (L.aspect || "16:9") + (L.image_zoom ? " · zoom" : "") + (L.multilang ? " · 7 languages" : ""), ...fmt));

  const narr = [];
  const n = el("div", "narr");
  n.appendChild(el("span", "lbl", "Narrator"));
  const vsel = el("select"); vsel.setAttribute("aria-label", "Voice");
  ttsVoiceOptions(L.tts_model).forEach(o => vsel.appendChild(new Option(o.label, o.value)));
  vsel.value = L.tts_voice; vsel.addEventListener("change", () => { L.tts_voice = vsel.value; persist(); });
  n.appendChild(vsel);
  n.appendChild(btn(svgIcon("play") + " Preview", () => previewTts(L, L.aspect === "9:16" ? "sketch-short" : "longform"), "sm"));
  n.appendChild(el("span", "lbl", "Engine"));
  const msel = el("select"); msel.setAttribute("aria-label", "TTS model");
  (OPT.longform_tts || []).forEach(o => msel.appendChild(new Option(o.label, o.value)));
  if ([...msel.options].some(o => o.value === L.tts_model)) msel.value = L.tts_model; else if (msel.options.length) { L.tts_model = msel.value; resetTtsVoice(L); }
  msel.addEventListener("change", () => { L.tts_model = msel.value; resetTtsVoice(L); persist(); render(); });
  n.appendChild(msel);
  narr.push(n);
  narr.push(el("div", "hint", esc(T.longform_voice_hint || "")));
  if (ttsProviderOf(L.tts_model) === "seed") narr.push(seedSettings(L));
  groups.appendChild(group("Narration", voiceLabel(L.tts_model, L.tts_voice), ...narr));

  // WHICH MODEL WROTE IT IS NOT THE DECISION THIS SCREEN IS ABOUT. Twelve vendor SKUs -
  // Claude Opus 4.8, GPT-5.6 Sol, Kimi K3, four flavours of Gemini Flash - and an API
  // parameter called "Reasoning mode", at the same weight as the two things a person making
  // a stickman explainer actually chooses. They are still here, one click away, for whoever
  // wants them.
  const dir = [];
  const dirAdv = el("details", "adv");
  dirAdv.appendChild(el("summary", "", "Which model writes it"));
  const dirBox = el("div", "fld-grid");
  dirAdv.appendChild(dirBox);
  dirBox.appendChild(selectField(T.longform_reasoning || "Reasoning model", OPT.longform_reasoning || [], L.reasoning_model, (x, initial) => {
    L.reasoning_model = x; L.reasoning_mode = reasoningOptions(x, L.reasoning_mode).value; if (!initial) render();
  }));
  const rm = reasoningOptions(L.reasoning_model, L.reasoning_mode);
  if (rm.options.length) { L.reasoning_mode = rm.value; dirBox.appendChild(selectField("Reasoning mode", rm.options, rm.value, x => L.reasoning_mode = x)); } else L.reasoning_mode = "";
  dir.push(dirAdv);
  dir.push(el("div", "subcap", "Speech"));
  dir.push(toggleField(T.longform_halt_speech || "Halt after speech (approve each part)", L.halt_after_speech !== false, v => L.halt_after_speech = v, T.longform_halt_hint || ""));
  groups.appendChild(group("Director", labelFor(OPT.longform_reasoning, L.reasoning_model), ...dir));

  groups.appendChild(group("Connection", "", connectionRow("higgsfield", "Higgsfield", "/higgsfield-status", "/higgsfield-login"),
    el("div", "hint", "The images are drawn on Higgsfield in a logged-in browser. Connect once; the session is kept.")));
  grid.appendChild(groups);
  page.appendChild(grid);
  main.appendChild(page);
  renderLongformDock();
}
function renderLongformDock() {
  const L = S.longform;
  const go = btn(T.create_longform || "Create Sketch Explainer", submitLongform);
  const problem = longformProblem();
  go.title = problem || "Ctrl+Enter in the script box also starts the run";
  dockWith([["Format", L.aspect || "16:9"], ["Narrator", voiceLabel(L.tts_model, L.tts_voice)], ["Director", labelFor(OPT.longform_reasoning, L.reasoning_model)],
            ["Speech", L.halt_after_speech !== false ? "approve each part" : "runs through"]], go, problem);
}
function buildLongformForm() {
  const L = S.longform;
  const fd = new FormData();
  fd.append("script", L.script || "");
  fd.append("tts_model", L.tts_model || firstVal(OPT.longform_tts) || "pro");
  fd.append("tts_voice", L.tts_voice || firstVal(OPT.tts_voice) || "");
  ["tts_voice_instruction", "tts_language", "tts_native_speed", "tts_volume", "tts_pitch", "tts_sample_rate", "tts_output_format"].forEach(k => {
    if (L[k] != null) fd.append(k, L[k]);
  });
  fd.append("aspect", L.aspect || "16:9");
  if (L.image_zoom === true) fd.append("image_zoom", "on");
  if (L.multilang === true) fd.append("multilang", "on");
  if (L.aspect === "9:16" && L.hook_intro === true) {
    fd.append("hook_intro", "on");
    if (L.hook_text) fd.append("hook_text", L.hook_text);
  }
  fd.append("reasoning_model", L.reasoning_model || firstVal(OPT.longform_reasoning) || "google/gemini-3.7-flash");
  fd.append("reasoning_mode", L.reasoning_mode || "");
  if (L.halt_after_speech !== false) fd.append("halt_after_speech", "on");
  return fd;
}
async function submitLongform() {
  if (submitting) return;
  const problem = longformProblem();
  if (problem) { toast(problem, true); const box = $("script-box"); if (box) box.focus(); return; }
  submitting = true;
  const go = $("go"); if (go) { go.disabled = true; go.textContent = "Starting…"; }
  askNotifyPermission();
  try {
    const r = await fetch("/longform-run", { method: "POST", body: buildLongformForm() });
    startJob(afterJobStart(r), "longform");
  } catch (e) { toast("The run could not start: " + e, true); if (go) { go.disabled = false; go.textContent = T.create_longform || "Create Sketch Explainer"; } }
  submitting = false;
}
async function openLongformPicker() {
  let d = { projects: [] }; try { d = await jget("/projects-list"); } catch (e) {}
  const rows = (d.projects || []).filter(p => p.longform);
  const body = el("div"); body.style.cssText = "padding:14px 16px; overflow:auto;";
  if (!rows.length) body.appendChild(el("p", "muted", "No Sketch Explainers yet."));
  const list = el("div", "recent-scripts"); list.style.maxHeight = "60vh";
  rows.forEach(p => {
    const b = el("button"); b.type = "button";
    b.innerHTML = `<b>${esc(p.title || p.slug)}</b><span>${esc((p.status || "") + " · " + (p.edited || ""))}</span>`;
    b.addEventListener("click", () => { m.remove(); openLongformProject(p); });
    list.appendChild(b);
  });
  body.appendChild(list);
  const m = modal("Sketch Explainers on the shelf", body);
}
/* a connection: the dot, the name, the one command */
function connectionRow(key, label, statusUrl, loginUrl) {
  const row = el("div", "conn");
  const dot = el("i", "dot"); row.appendChild(dot);
  const txt = el("span", "", esc(label)); row.appendChild(txt);
  const b = btn("Connect", async () => { b.disabled = true; try { await jpost(loginUrl, {}); } catch (e) {} b.disabled = false; setTimeout(paint, 1500); }, "sm");
  row.appendChild(b);
  const paint = async () => {
    try {
      const st = await jget(statusUrl);
      dot.className = "dot" + (st.busy ? " busy" : st.ready ? " on" : "");
      txt.textContent = label + " · " + (st.busy ? (T.busy || "busy") : st.ready ? (T.connected || "connected") : (T.not_connected || "not connected"));
      b.textContent = st.ready ? (T.reconnect || "Reconnect") : "Connect";
    } catch (e) {}
  };
  paint();
  return row;
}

/* ------------------------------------------------------------------ the Physics Bench (/?flow=physics)
   One scene, one value swept, rendered in Blender. Posts the same JSON body to /physics-run as the
   older console (chat-shell.js physShotCard). */
let PHYS_LIB = null, PHYS_LIB_ERR = "", physLibPending = false;
function physicsDefaults() { return { scene: "", params: {}, sweep: true, sweepValues: "", seconds: 0, samples: 24, prompt: "", brief_model: PHYS_BRIEF_MODELS[0].id }; }
function physCap(s) {
  // `s || ""` turned false, 0 and "" into the same empty label
  const t = String(s === undefined || s === null ? "" : s).replace(/_/g, " ");
  return t.charAt(0).toUpperCase() + t.slice(1);
}
function physSceneOf(P) { return (PHYS_LIB || []).find(s => s.scene === P.scene) || null; }
function physSweepValues(sc, P) {
  const raw = String(P.sweepValues || "").trim();
  if (!raw) return (sc && sc.sweep ? sc.sweep.values : []) || [];
  return raw.split(/[,;\s]+/).map(Number).filter(n => isFinite(n));
}
function physTakeCount(sc, P) {
  if (!sc || !sc.sweep || !P.sweep) return 1;
  const v = physSweepValues(sc, P);
  return v.length || (sc.sweep.values || []).length || 1;
}
function physFetchLibrary(then) {
  if (physLibPending) return;
  physLibPending = true;
  jget("/physics-scenes").then(d => { PHYS_LIB = d.scenes || []; PHYS_LIB_ERR = d.error || ""; })
    .catch(e => { PHYS_LIB = []; PHYS_LIB_ERR = String(e); })
    .then(() => { physLibPending = false; then && then(); });
}
function physParamControl(spec, P) {
  const val = P.params[spec.key] !== undefined ? P.params[spec.key] : spec.default;
  if (spec.choices && spec.choices.length) {
    // A BOOLEAN PARAMETER RENDERED AS "True" AND A BLANK BUTTON, WITH NEITHER SELECTED.
    // The choices arrive from the scene file as JSON true/false. physCap(false) went through
    // `String(s || "")`, which swallows false into an empty 20px button, and the option's value
    // stayed a boolean while the current value was stringified - so `false === "false"` was
    // never true and the segment showed no selection at all. The Python literal True was the
    // visible label for the other half.
    const asOption = ch => [String(ch),
      typeof ch === "boolean" ? (ch ? "On" : "Off") : physCap(ch)];
    return segField(physCap(spec.label), spec.choices.map(asOption), String(val), v => {
      const ch = spec.choices.find(c => String(c) === v); P.params[spec.key] = ch === undefined ? v : ch;
    });
  }
  if (spec.range && spec.range.length === 2) {
    const [lo, hi] = spec.range.map(Number);
    const whole = Number.isInteger(lo) && Number.isInteger(hi) && Number.isInteger(Number(spec.default));
    const step = whole ? 1 : Math.max(0.01, (hi - lo) / 200);
    const f = rangeField(physCap(spec.label), lo, hi, step, val, x => String(whole ? Math.round(+x) : Math.round(+x * 100) / 100),
      x => { P.params[spec.key] = whole ? Math.round(+x) : Math.round(+x * 100) / 100; }, spec.note || "");
    if (spec.key === "seed") {
      const dice = btn("New seed", () => { const n = Math.floor(Math.random() * (hi - lo + 1)) + lo; P.params[spec.key] = n; persist(); render(); }, "sm ghost");
      dice.title = "A different seed is a visibly different take";
      f.querySelector(".range-row").appendChild(dice);
    }
    return f;
  }
  return null;
}
function renderPhysics(main) {
  const P = S.physics;
  if (PHYS_LIB === null) physFetchLibrary(() => { if (S.view === "home" && S.mode === "physics") render(); });
  const page = el("div", "page");
  if (pendingDraft) page.appendChild(restoreBanner());
  page.appendChild(pageHead("Physics Bench", T.mode_physics_d || "", "One cheap frame is rendered first - nothing long starts until you approve it."));
  const grid = el("div", "run-grid");

  const p = el("section", "panel");
  const head = el("div", "panel-head"); head.appendChild(el("span", "label", "Simulation"));
  const cap = el("span", "cap", PHYS_LIB ? `${PHYS_LIB.length} scenes on the bench` : "reading the bench…"); head.appendChild(cap); p.appendChild(head);
  p.appendChild(el("div", "hint", esc(T.physics_intro || "Pick what gets simulated.")));
  const tiles = el("div", "phys-grid"); tiles.setAttribute("role", "listbox"); tiles.setAttribute("aria-label", "Scenes");
  if (!PHYS_LIB) tiles.appendChild(el("div", "cap", "Reading the scene files…"));
  else if (!PHYS_LIB.length) tiles.appendChild(el("div", "cap", esc(PHYS_LIB_ERR || "No scenes found in physics_mode/scenes/lib.")));
  else PHYS_LIB.forEach(sc => {
    const on = P.scene === sc.scene;
    const b = el("button", "phys-tile" + (on ? " on" : "")); b.type = "button"; b.setAttribute("role", "option"); b.setAttribute("aria-selected", on ? "true" : "false");
    const takes = sc.sweep ? (sc.sweep.values || []).length : 0;
    const kind = takes > 1 ? `${takes} takes` : (sc.loop ? "loops" : "one take");
    const sweepLine = sc.sweep ? `${sc.sweep.param.replace(/_/g, " ")} · ${(sc.sweep.values || []).join(" · ")}${sc.sweep.unit || ""}` : "";
    b.innerHTML = `<span class="pt-head"><b>${esc(sc.title)}</b><i>${esc(kind)}</i></span><p title="${esc(sc.does)}">${esc(sc.does)}</p>` +
      // The sweep line is the only thing separating take 1 from take 3 on a "3 takes" card, and
      // it is clipped to 109px against 225 - "blade count - 4000 - 40000 - 200000 blades" shows
      // less than half of itself. It cannot be widened without wrapping the tile, so it can at
      // least be read on hover.
      `<span class="pt-foot"><code title="${esc(sweepLine)}">${esc(sweepLine)}</code>` +
      `<i>${sc.params.length} setting${sc.params.length === 1 ? "" : "s"}</i></span>`;
    b.addEventListener("click", () => { P.scene = on ? "" : sc.scene; P.params = {}; P.sweepValues = ""; P.seconds = 0; if (P.scene) P.prompt = ""; persist(); render(); });
    tiles.appendChild(b);
  });
  p.appendChild(tiles);
  p.appendChild(el("div", "subcap", "or describe it and let the app choose"));
  const ta = el("textarea"); ta.placeholder = T.physics_prompt_ph || ""; ta.value = P.prompt || ""; ta.rows = 2; ta.setAttribute("aria-label", T.physics_prompt_label || "Describe the scene");
  ta.addEventListener("input", () => { P.prompt = ta.value; if (ta.value.trim() && P.scene) { P.scene = ""; render(); } persist(); renderPhysicsDock(); });
  p.appendChild(ta);
  if (!P.scene) p.appendChild(selectField(T.physics_brief_model || "Model that writes the scene brief", PHYS_BRIEF_MODELS.map(m => ({ value: m.id, label: m.t })),
    P.brief_model || PHYS_BRIEF_MODELS[0].id, v => P.brief_model = v, "Only used if no scene in the library fits; the library is tried first, always."));
  grid.appendChild(p);

  const groups = el("div", "groups");
  const sc = physSceneOf(P);
  const shot = [];
  if (sc) {
    shot.push(el("div", "hint", esc(sc.does)));
    if (sc.sweep) {
      shot.push(segField("Format", [["comparison", "Comparison"], ["single", "Single take"]], P.sweep ? "comparison" : "single", v => { P.sweep = v === "comparison"; }));
      shot.push(el("div", "hint", P.sweep ? "The same setup rendered once per value and joined, each take labelled. The contrast IS the video."
        : `One event, rendered once. ${physCap(sc.sweep.param)} stays at its default.`));
      if (P.sweep) shot.push(textField(`${physCap(sc.sweep.param)} per take${sc.sweep.unit ? " (" + sc.sweep.unit + ")" : ""}`, P.sweepValues || (sc.sweep.values || []).join(", "),
        v => P.sweepValues = v, "", T.physics_hint || "Comma separated. Three or four reads best - each one is a full render."));
    }
    const swept = P.sweep && sc.sweep ? sc.sweep.param : "";
    const knobs = sc.params.filter(x => x.key !== swept).sort((a, b) => (a.key === "seed") - (b.key === "seed"));
    const main4 = knobs.slice(0, 4), rest = knobs.slice(4);
    main4.forEach(spec => { const r = physParamControl(spec, P); if (r) shot.push(r); });
    if (rest.length) {
      shot.push(el("div", "subcap", `${rest.length} more setting${rest.length === 1 ? "" : "s"}`));
      rest.forEach(spec => { const r = physParamControl(spec, P); if (r) shot.push(r); });
    }
  } else shot.push(el("div", "hint", P.prompt ? "The app picks the scene from your description, or writes one from scratch." : "Pick a scene on the left, or describe one."));
  groups.appendChild(group("Shot", sc ? sc.title : (P.prompt ? "described" : ""), ...shot));

  const secDefault = sc && sc.seconds ? Number(sc.seconds) : 4;
  const ren = [];
  ren.push(rangeField("Seconds per take", 2, 12, 0.5, P.seconds || secDefault, x => x + " s", x => P.seconds = Number(x)));
  ren.push(segField("Render quality", PHYS_QUALITY.map(([s, t]) => [String(s), t]), String(P.samples || 24), v => P.samples = Number(v)));
  ren.push(el("div", "hint", PHYS_QUALITY.map(([, t, d]) => `${t}: ${d}`).join(" · ")));
  groups.appendChild(group("Render", `${physTakeCount(sc, P)} × ${P.seconds || secDefault} s · ${(PHYS_QUALITY.find(q => q[0] === Number(P.samples || 24)) || PHYS_QUALITY[1])[1]}`, ...ren));
  grid.appendChild(groups);
  page.appendChild(grid);
  main.appendChild(page);
  renderPhysicsDock();
}
function physicsProblem() { const P = S.physics; return (!P.scene && !(P.prompt || "").trim()) ? "Pick a simulation or describe one." : ""; }
function renderPhysicsDock() {
  const P = S.physics; const sc = physSceneOf(P);
  dockWith([["Scene", sc ? sc.title : (P.prompt ? "described" : "-")], ["Takes", String(physTakeCount(sc, P))], ["Quality", String(P.samples || 24) + " samples"]],
    btn("Render the preview frame", submitPhysics), physicsProblem());
}
function buildPhysicsBody() {
  const P = S.physics; const sc = physSceneOf(P);
  const secDefault = sc && sc.seconds ? Number(sc.seconds) : 4;
  const body = { seconds: P.seconds || secDefault, samples: P.samples || 24 };
  if (P.scene) {
    body.scene = P.scene; body.params = P.params; body.sweep = !!P.sweep;
    if (sc && sc.sweep && P.sweep) body.sweep_values = physSweepValues(sc, P);
  } else { body.prompt = P.prompt || ""; body.brief_model = P.brief_model || PHYS_BRIEF_MODELS[0].id; }
  return body;
}
async function submitPhysics() {
  if (submitting) return;
  const problem = physicsProblem(); if (problem) { toast(problem, true); return; }
  submitting = true;
  const go = $("go"); if (go) { go.disabled = true; go.textContent = "Starting…"; }
  askNotifyPermission();
  try {
    const d = await jpost("/physics-run", buildPhysicsBody());
    if (d.error) throw new Error(d.error);
    startJob(d.job_id, "physics");
  } catch (e) { toast("The render could not start: " + e, true); if (go) { go.disabled = false; go.textContent = "Render the preview frame"; } }
  submitting = false;
}

/* ------------------------------------------------------------------ the Story Station (/reddit) */
function renderReddit(main) {
  const R = S.reddit;
  const page = el("div", "page");
  page.appendChild(pageHead("Story Station", T.mode_reddit_d || "A Reddit story, read over parkour footage.", T.reddit_intro || ""));
  const grid = el("div", "run-grid");
  const p = el("section", "panel");
  const head = el("div", "panel-head"); head.appendChild(el("span", "label", "Stories"));
  const cap = el("span", "cap", (R.stories || []).length ? `${R.stories.length} found` : ""); head.appendChild(cap); p.appendChild(head);
  const list = el("div", "stories");
  const paint = () => {
    list.innerHTML = "";
    (R.stories || []).forEach((st, i) => {
      const box = el("article", "cand story" + (R.story && R.story.id === st.id ? " on" : ""));
      box.appendChild(el("b", "", esc(st.title || ("Story " + (i + 1)))));
      box.appendChild(el("p", "muted", esc(String(st.body || st.text || "").slice(0, 260)) + "…"));
      box.appendChild(btn("Make this one", async (ev) => {
        ev.currentTarget.disabled = true; R.story = st; persist();
        try {
          const d = await jpost("/reddit-generate", { story: st, story_id: st.id });
          if (d.error) throw new Error(d.error);
          const jid = (d.job || "").split("id=")[1] || d.job_id;
          if (!jid) throw new Error("no job id");
          startJob(jid, "reddit");
        } catch (e) { toast("Could not start: " + e, true); ev.currentTarget.disabled = false; }
      }, "sm primary"));
      list.appendChild(box);
    });
    if (!(R.stories || []).length) list.appendChild(el("div", "cap", "No stories yet - find five."));
  };
  paint();
  p.appendChild(list);
  grid.appendChild(p);
  const groups = el("div", "groups");
  groups.appendChild(group("How it is made", "", el("div", "hint", "The story is read by an AI narrator over Minecraft parkour footage. No captions, no other input.")));
  grid.appendChild(groups);
  page.appendChild(grid);
  main.appendChild(page);
  const go = btn(T.find_stories || "Find 5 stories", async () => {
    go.disabled = true; go.textContent = "Searching…";
    try {
      const d = await jpost("/reddit-discover", {});
      if (d.error) throw new Error(d.error);
      R.stories = d.stories || []; R.story = null; persist(); paint();
      cap.textContent = `${R.stories.length} found`;
    } catch (e) { toast("Could not find stories: " + e, true); }
    go.disabled = false; go.textContent = T.find_stories || "Find 5 stories";
  });
  dockWith([["Stories", String((R.stories || []).length)]], go, "");
}

/* ------------------------------------------------------------------ the Action Edit (Effects Station)
   The one master that takes THREE clips. Posts clip1/clip2/clip3 by name, the title, the
   overlay, the ramps, the editing brain, the trim plan and the two switches to
   /action-edit-run - the same fields as the older console. */
const ACTION_FILES = [null, null, null];
const ACTION_META = [null, null, null];     // { duration, url, keep: [start, end] } per clip
function actionDefaults() { return { title: "", text: "", ramps: "2", editorModel: "google/gemini-3.5-flash-lite", hook: true, loop: false }; }
function actionTime(v) { v = Math.max(0, +v || 0); const m = Math.floor(v / 60), s = v - m * 60; return `${m}:${s.toFixed(1).padStart(4, "0")}`; }
function actionPanel() {
  const A = S.action;
  const p = el("section", "panel");
  const head = el("div", "panel-head"); head.appendChild(el("span", "label", "Three clips"));
  const cap = el("span", "cap"); head.appendChild(cap); p.appendChild(head);
  p.appendChild(el("div", "hint", "Clip 1, 2 and 3 of one Short, in story order. Keep one stretch of each; the editing agent finds the beats, the cold open and the ramps."));
  const row = el("div", "action-grid");
  const ready = () => ACTION_FILES.filter(Boolean).length;
  const paintCap = () => { cap.textContent = `${ready()}/3 attached`; };
  [0, 1, 2].forEach(i => {
    const col = el("div", "action-col");
    const drop = el("div", "drop sm"); drop.tabIndex = 0; drop.setAttribute("role", "button");
    const f = ACTION_FILES[i];
    const paintDrop = () => {
      const file = ACTION_FILES[i];
      const size = file && (file.size >= 1048576 ? (file.size / 1048576).toFixed(1) + " MB" : Math.max(1, Math.round(file.size / 1024)) + " KB");
      drop.innerHTML = file ? `<b>Clip ${i + 1}</b><span class="cap">${esc(file.name)} · ${size}</span>` : `<b>Clip ${i + 1}</b><span class="cap">Click or drop a video</span>`;
      drop.setAttribute("aria-label", file ? `Clip ${i + 1}: ${file.name} - click to replace` : `Attach clip ${i + 1}`);
    };
    paintDrop();
    const take = (file) => {
      if (!file) return;
      if (ACTION_META[i] && ACTION_META[i].url) URL.revokeObjectURL(ACTION_META[i].url);
      ACTION_FILES[i] = file; ACTION_META[i] = { duration: 0, url: URL.createObjectURL(file), keep: null };
      render();
    };
    const accept = "video/mp4,video/quicktime,video/webm,video/x-matroska,.mp4,.mov,.webm,.mkv";
    drop.addEventListener("click", () => pickFile(accept, take));
    drop.addEventListener("keydown", e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); pickFile(accept, take); } });
    ["dragenter", "dragover"].forEach(ev => drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.add("over"); }));
    ["dragleave", "drop"].forEach(ev => drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.remove("over"); }));
    drop.addEventListener("drop", e => take(e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0]));
    col.appendChild(drop);
    if (f) {
      const meta = ACTION_META[i];
      const vid = el("video"); vid.controls = true; vid.muted = true; vid.preload = "metadata"; vid.src = meta.url; vid.playsInline = true;
      col.appendChild(vid);
      const trim = el("div", "action-trim");
      const paintTrim = () => {
        trim.innerHTML = "";
        if (!meta.duration) { trim.appendChild(el("span", "cap", "Reading the length…")); return; }
        const keep = meta.keep || [0, meta.duration];
        const from = el("input"); from.type = "number"; from.min = "0"; from.max = String(meta.duration); from.step = "0.1"; from.value = keep[0].toFixed(1); from.setAttribute("aria-label", `Clip ${i + 1} keep from`);
        const to = el("input"); to.type = "number"; to.min = "0"; to.max = String(meta.duration); to.step = "0.1"; to.value = keep[1].toFixed(1); to.setAttribute("aria-label", `Clip ${i + 1} keep to`);
        const sum = el("span", "cap");
        const commit = () => {
          const a = Math.max(0, Math.min(meta.duration, +from.value || 0)), b2 = Math.max(0, Math.min(meta.duration, +to.value || meta.duration));
          meta.keep = a < b2 ? [a, b2] : [0, meta.duration];
          sum.textContent = `${actionTime(meta.keep[1] - meta.keep[0])} kept of ${actionTime(meta.duration)}`;
        };
        from.addEventListener("change", commit); to.addEventListener("change", commit);
        const mark = (which) => btn(which === 0 ? "In = here" : "Out = here", () => { (which === 0 ? from : to).value = vid.currentTime.toFixed(1); commit(); }, "sm ghost");
        trim.appendChild(el("span", "lbl", "Keep")); trim.appendChild(from); trim.appendChild(mark(0));
        trim.appendChild(el("span", "lbl", "to")); trim.appendChild(to); trim.appendChild(mark(1));
        trim.appendChild(sum); commit();
      };
      vid.addEventListener("loadedmetadata", () => { meta.duration = vid.duration || 0; paintTrim(); });
      if (meta.duration) paintTrim(); else paintTrim();
      col.appendChild(trim);
      col.appendChild(btn("Remove clip " + (i + 1), () => {
        try { vid.pause(); } catch (e) {} vid.removeAttribute("src"); vid.load();
        if (meta.url) URL.revokeObjectURL(meta.url);
        ACTION_FILES[i] = null; ACTION_META[i] = null; render();
      }, "sm ghost"));
    }
    row.appendChild(col);
  });
  paintCap();
  p.appendChild(row);
  return p;
}
function actionGroups() {
  const A = S.action;
  const g = el("div", "groups");
  g.appendChild(group("The short", A.title || "untitled",
    textField("Title", A.title, v => A.title = v, "Downhill Skateboard"),
    textField("Text overlay (optional)", A.text, v => A.text = v, "he sent it", "3-5 words. Shown from 0.4s to 2.4s, near the top, clear of the platform's own buttons."),
    selectField("Speed ramps", [{ value: "2", label: "Two - on the strongest trick peaks" }, { value: "1", label: "One - only the single biggest peak" }, { value: "0", label: "None - keep it all at full speed" }],
      A.ramps || "2", v => A.ramps = v)));
  g.appendChild(group("Editing brain", "", selectField("", [
    { value: "google/gemini-3.5-flash-lite", label: "AI editor · Gemini 3.5 Flash Lite" },
    { value: "google/gemini-3.1-flash-lite", label: "AI editor · Gemini 3.1 Flash Lite" },
    { value: "openai/gpt-5.6-luna", label: "AI editor · GPT-5.6 Luna" },
    { value: "openai/gpt-5.6-terra", label: "AI editor · GPT-5.6 Terra" },
    { value: "openai/gpt-5.6-sol", label: "AI editor · GPT-5.6 Sol" },
    { value: "local", label: "Local cut engine · no API" },
  ], A.editorModel || "google/gemini-3.5-flash-lite", v => A.editorModel = v),
    el("div", "hint", "The agent sees a contact sheet of the actual clips and decides the hook, story order and ramps. It never adds library SFX."),
    toggleField("Cold open", A.hook !== false, v => A.hook = v),
    toggleField("Loop shaping", A.loop === true, v => A.loop = v),
    el("div", "hint", "Cuts land on movement. The original action sound stays intact. The series grade and a -14 LUFS master run locally.")));
  return g;
}
function buildActionForm() {
  const A = S.action;
  const fd = new FormData();
  ACTION_FILES.forEach((f, i) => fd.append(`clip${i + 1}`, f, f.name));
  fd.append("title", A.title || "Action Edit");
  fd.append("overlay_text", A.text || "");
  fd.append("max_ramps", A.ramps || "2");
  fd.append("editor_model", A.editorModel || "google/gemini-3.5-flash-lite");
  const trimPlan = {};
  ACTION_META.forEach((m, i) => { if (m && m.keep && m.duration && (m.keep[0] > 0.05 || m.keep[1] < m.duration - 0.05)) trimPlan[String(i + 1)] = [m.keep]; });
  fd.append("trim_plan", JSON.stringify(trimPlan));
  if (A.hook !== false) fd.append("hook_enabled", "on");
  if (A.loop === true) fd.append("loop_shaping", "on");
  return fd;
}
async function submitActionEdit() {
  if (submitting) return;
  if (ACTION_FILES.filter(Boolean).length < 3) { toast("Action Edit needs all three clips of the Short.", true); return; }
  submitting = true;
  const go = $("go"); if (go) { go.disabled = true; go.textContent = "Uploading…"; }
  askNotifyPermission();
  try {
    const r = await fetch("/action-edit-run", { method: "POST", body: buildActionForm() });
    startJob(afterJobStart(r), "actionedit");
  } catch (e) { toast((T.upload_failed || "Upload failed") + " " + e, true); if (go) { go.disabled = false; go.textContent = "Build the edit"; } }
  submitting = false;
}

/* ------------------------------------------------------------------ the gates the other machines raise
   The longform speech parts (approve each, decline regenerates that part; polish presets) and
   the physics preview frame. Same endpoints, same query strings as the older console. */
function paintExtraGates(d, g) {
  const parts = d.lf_parts || [];
  if (parts.length) {
    const sig = "lf:" + JSON.stringify(parts.map(p => [p.index, p.state, p.url, p.preset]));
    if (g.dataset.sig === sig) return true;
    g.dataset.sig = sig; g.innerHTML = "";
    const c = el("section", "gate");
    c.appendChild(el("h2", "h2", "Your voiceover parts are ready"));
    c.appendChild(el("p", "muted", esc(T.lf_parts_ready || "Approve each part - declining re-generates that part.")));
    const pending = parts.filter(p => p.state === "pending");
    const total = parts.reduce((s, p) => s + (+p.dur || 0), 0);
    const bulk = el("div", "row");
    bulk.appendChild(el("span", "lbl muted", `${parts.length} ${T.lf_parts_word || "parts"}` + (total > 0 ? ` · ${clock(total)} ${T.lf_total || "total"}` : "")));
    const decide = async (index, action, extra) => {
      await fetch("/longform-speech-decide?id=" + encodeURIComponent(S.jobId) + "&part=" + encodeURIComponent(index) + "&action=" + action + (extra || ""), { method: "POST" });
      setTimeout(pollJob, 700);
    };
    if (pending.length > 1) {
      const all = async (action, b) => { [...bulk.querySelectorAll("button")].forEach(x => x.disabled = true); b.textContent = "…"; for (const p of pending) await decide(p.index, action); };
      bulk.appendChild(btn(svgIcon("check") + ` ${T.lf_approve_all || "Approve all"} (${pending.length})`, ev => all("approve", ev.currentTarget), "sm primary"));
      bulk.appendChild(btn(T.lf_decline_all || "Decline & regenerate all", ev => all("decline", ev.currentTarget), "sm danger"));
    }
    c.appendChild(bulk);
    const list = el("div", "lf-parts");
    parts.forEach(p => {
      const row = el("div", "lf-part" + (p.state === "approved" ? " done" : ""));
      row.appendChild(el("span", "cap", esc(`${T.lf_part || "Part"} ${p.index + 1}` + (+p.dur > 0 ? ` · ${clock(p.dur)}` : "") +
        (p.state === "approved" ? ` · ${T.lf_approved || "Approved"}` : p.state === "regenerating" ? ` · ${T.lf_regenerating || "Regenerating..."}` : ""))));
      row.appendChild(el("p", "", esc((p.text || "").slice(0, 220))));
      if (p.url && p.state !== "regenerating") { const au = el("audio"); au.controls = true; au.preload = "none"; au.src = p.url; row.appendChild(au); }
      const acts = el("div", "row");
      if (p.state !== "regenerating" && (OPT.voice_presets || []).length) {
        acts.appendChild(el("span", "lbl muted", "Polish"));
        const sel = el("select"); sel.style.width = "auto";
        (OPT.voice_presets || []).forEach(o => { const opt = new Option(o.label, o.value); opt.title = o.hint || ""; sel.appendChild(opt); });
        sel.value = p.preset || "none";
        sel.addEventListener("change", () => { sel.disabled = true; decide(p.index, "polish", "&preset=" + encodeURIComponent(sel.value)); });
        acts.appendChild(sel);
      }
      if (p.state === "pending") {
        acts.appendChild(btn(svgIcon("check") + " " + (T.lf_approve || "Approve"), ev => { ev.currentTarget.disabled = true; decide(p.index, "approve"); }, "sm primary"));
        acts.appendChild(btn(T.lf_decline || "Decline & regenerate", ev => { ev.currentTarget.disabled = true; decide(p.index, "decline"); }, "sm danger"));
      }
      row.appendChild(acts);
      list.appendChild(row);
    });
    c.appendChild(list);
    g.appendChild(c);
    c.scrollIntoView({ behavior: REDUCED ? "auto" : "smooth", block: "nearest" });
    return true;
  }
  if (d.physics_preview_url) {
    const sig = "phys:" + d.physics_preview_url;
    if (g.dataset.sig === sig) return true;
    g.dataset.sig = sig; g.innerHTML = "";
    const c = el("section", "gate");
    c.appendChild(el("h2", "h2", esc(T.physics_approve_t || "Does this shot look right?")));
    c.appendChild(el("p", "muted", esc(T.physics_approve_d || "")));
    const shot = el("img", "phys-shot"); shot.src = d.physics_preview_url; shot.alt = "The preview frame"; c.appendChild(shot);
    const pk = d.physics_pick;
    if (pk) {
      const facts = el("div", "chips");
      facts.appendChild(el("span", "chip", esc(pk.title || pk.scene || "")));
      if (pk.sweep && (pk.sweep.values || []).length) facts.appendChild(el("span", "chip", esc(`${String(pk.sweep.param || "").replace(/_/g, " ")}: ${(pk.sweep.values || []).join(" · ")}${pk.sweep.unit || ""}`)));
      const sweptKey = pk.sweep ? pk.sweep.param : "";
      Object.keys(pk.params || {}).filter(k => k !== sweptKey).slice(0, 8).forEach(k => facts.appendChild(el("span", "chip", esc(`${k.replace(/_/g, " ")} ${String(pk.params[k]).replace(/_/g, " ")}`))));
      c.appendChild(facts);
    }
    const row = el("div", "row");
    const decide = async (action, b, label) => {
      b.disabled = true; b.textContent = "…";
      try {
        await fetch("/approve-physics?id=" + encodeURIComponent(S.jobId) + "&action=" + action, { method: "POST" });
        if (action === "retry") { g.innerHTML = ""; delete g.dataset.sig; }
        setTimeout(pollJob, 700);
      } catch (e) { b.disabled = false; b.textContent = label; }
    };
    row.appendChild(btn(svgIcon("check") + " " + (T.physics_approve_yes || "Render it"), ev => decide("approve", ev.currentTarget, T.physics_approve_yes), "primary"));
    row.appendChild(btn("Another take", ev => decide("retry", ev.currentTarget, "Another take"), "sm"));
    row.appendChild(btn(T.physics_approve_no || "No, stop here", ev => decide("decline", ev.currentTarget, T.physics_approve_no), "sm danger"));
    c.appendChild(row);
    g.appendChild(c);
    c.scrollIntoView({ behavior: REDUCED ? "auto" : "smooth", block: "nearest" });
    return true;
  }
  return false;
}

/* ------------------------------------------------------------------ boot */
function boot() {
  const init = BOOT.initial || {};
  const drafts = draftCandidates().filter(draftIsWorthRestoring);
  ensureLayers();
  let showSplash = false;
  if (init.job) {
    // a job deep link opens the wait screen; the draft (if any) waits on the run page
    if (drafts.length) { const d = drafts[0]; if (d.jobId === init.job) adoptDraft(d); else pendingDraft = d; }
    S.view = "wait"; S.jobId = init.job; S.jobStatus = "running";
  } else if (init.flow && ["sfx", "visual", "captions", "asmr", "actionedit"].includes(init.flow)) {
    S.mode = "enhance"; S.enhance.kind = init.flow; S.view = "home"; S.slide = MODES.findIndex(m => m.id === "enhance");
    if (drafts.length) pendingDraft = drafts[0];
  } else if (init.flow && ["challenge", "longform", "physics", "reddit"].includes(init.flow)) {
    // the other machines, reached by their own route: straight onto their screen
    if (drafts.length) { const d = drafts[0]; if (d.mode === init.flow) adoptDraft(d); else pendingDraft = d; }
    S.mode = init.flow; S.view = "home"; S.slide = Math.max(0, MODES.findIndex(m => m.id === init.flow));
  } else if (init.view === "assets") {
    S.view = "assets"; S.assets.q = init.q || ""; S.assets.hidden = !!init.hidden;
  } else if (init.project) {
    loadProject(init.project); return;
  } else {
    if (drafts.length && !init.new) pendingDraft = drafts[0];
    // The machine starts up when you walk in the front door. `splash` itself only plays it once
    // per browser session (BOOTED_KEY), so this is the first load of the tab and nothing else.
    S.view = SHOWROOM_3D ? "showroom" : "start"; showSplash = SHOWROOM_3D;
    // client-side links straight to one machine: #machine=<id> (showroom) or #screen=<id>
    // (its run page, v3 modes only). No boot splash on either.
    const h = new URLSearchParams(location.hash.replace(/^#/, ""));
    const want = MODES.findIndex(m => m.id === (h.get("machine") || h.get("screen")));
    if (want >= 0) {
      S.slide = want; showSplash = false;
      if (h.get("screen") && MODES[want].page === "v3") { S.mode = MODES[want].mode; S.view = "home"; }
    }
  }
  const start = () => {
    render();
    refreshJobs(); setInterval(refreshJobs, 12000);
    document.addEventListener("visibilitychange", () => { if (!document.hidden && S.view === "wait") pollJob(); });
  };
  if (showSplash) { setBackdrop(MODES[S.slide]); splash(start); } else start();
}

async function openProjectSearch() {
  if (document.querySelector(".studio-command")) return;
  const body = el("div", "studio-command");
  const input = el("input"); input.type = "search"; input.placeholder = "Search by project name…";
  input.setAttribute("aria-label", "Search projects"); input.autocomplete = "off"; body.appendChild(input);
  const results = el("div", "studio-command-results"); body.appendChild(results);
  results.appendChild(el("p", "hint", "Loading projects…"));
  let items = [];
  let dialog;
  const draw = () => {
    results.innerHTML = "";
    const q = input.value.trim().toLowerCase();
    items.filter(item => ((item.title || "") + " " + (item.slug || "")).toLowerCase().includes(q)).slice(0, 12).forEach(item => {
      const detail = [item.status, item.mode].filter(Boolean).join(" · ") || item.slug;
      const choice = btn(`<span><b>${esc(item.title || item.slug)}</b><small>${esc(detail)}</small></span><span aria-hidden="true">↗</span>`, () => {
        dialog.querySelector('.modal-head button').click(); loadProject(item.slug);
      }, "studio-command-item");
      results.appendChild(choice);
    });
    if (!results.children.length) results.appendChild(el("p", "hint", q ? "No project matches that name." : "No projects yet."));
  };
  input.addEventListener("input", draw);
  input.addEventListener("keydown", e => { if (e.key === "ArrowDown") { e.preventDefault(); results.querySelector('button')?.focus(); } else if (e.key === "Enter") { e.preventDefault(); results.querySelector('button')?.click(); } });
  results.addEventListener("keydown", e => {
    const choices = [...results.querySelectorAll('button')], i = choices.indexOf(document.activeElement);
    if (e.key === "ArrowDown") { e.preventDefault(); choices[Math.min(i+1,choices.length-1)]?.focus(); }
    else if (e.key === "ArrowUp") { e.preventDefault(); if (i <= 0) input.focus(); else choices[i-1].focus(); }
  });
  dialog = modal("Find a project", body); input.focus();
  try {
    const data = await jget("/projects-list"); items = data.projects || []; draw();
  } catch (e) {
    results.innerHTML = ""; results.appendChild(el("p", "hint", "Projects could not be loaded."));
  }
}
document.addEventListener("keydown", e => {
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") { e.preventDefault(); openProjectSearch(); }
});
function openStudioMode(m) {
  stopPolling();
  S.mode = m.mode; S.slide = Math.max(0, MODES.indexOf(m)); S.view = "home";
  history.replaceState(null, "", ["clip", "ai"].includes(m.mode) ? withUi("/") + "#screen=" + m.id : routeFor(m.mode));
  persist(); render(); $("v3-main").scrollTop = 0; $("v3-main").focus();
}
// The home is the machine showroom, not the editorial studio page. The owner designed that
// carousel shot by shot (one machine, vertical non-looping scroll, the shutter jolt, the push-in
// through the PC's own screen) and asked for it back; renderStudioHome stays for reference.
/* ------------------------------------------------------ the start screen
   The showroom is a lobby: a photographed desk you fly through before any work starts. It is
   kept - `?ui=showroom` still flies it - but it is no longer what opening the app means.

   What this machine actually is, most of the time, is BUSY. A render holds the GPU for twenty
   minutes; a scrape runs longer. So the first thing on the page is not a menu, it is the state
   of the machine: what is running, how far in, how long it has been. When nothing runs the same
   strip becomes the line that starts something. One object, two states - that is the whole idea
   of this screen, and everything around it stays quiet.

   The stations are labelled by what they take and what they give back, because that is how you
   actually pick one: I have a script and I want a short. Not by a number in a sequence - they
   are not a sequence. */
const SHOWROOM_3D = (() => {
  try { return new URLSearchParams(location.search).get("ui") === "showroom"; } catch (e) { return false; }
})();

function renderStartScreen(main) {
  const page = el("div", "start");

  // No masthead. The wordmark, the search and the Library link already live in the top bar two
  // centimetres above; repeating them cost 130px of the most valuable region on the screen and
  // told the reader nothing. The strip is the first thing here now.
  const strip = el("section", "deckbar");
  strip.id = "start-deck";
  page.appendChild(strip);
  paintDeck(strip);

  const secA = el("section", "start-sec");
  secA.appendChild(sectionLabel("Stations"));
  const grid = el("div", "stations");
  MODES.forEach((m, i) => {
    const card=stationCard(m);
    card.style.setProperty('--station-order',i);
    card.appendChild(el('span','station-number',String(i+1).padStart(2,'0')));
    card.querySelector('.station-body').appendChild(el('span','station-open','Open station <span aria-hidden="true">↗</span>'));
    grid.appendChild(card);
  });
  secA.appendChild(grid);
  page.appendChild(secA);

  const secB = el("section", "start-sec");
  secB.appendChild(sectionLabel("Continue"));
  const cont = el("div", "continue");
  cont.id = "start-continue";
  for (let i = 0; i < 4; i++) cont.appendChild(el("div", "cont-card is-skeleton"));
  secB.appendChild(cont);
  page.appendChild(secB);
  fillContinue(cont);

  main.appendChild(page);
  disposeStartMotion = mountStationMotion(grid);
}

function mountStationMotion(grid) {
  const cards=[...grid.children], videos=new Map();
  let selected=0, disposed=false, hoveringTimer=0, previewInView=true;
  const wide=matchMedia('(min-width: 1000px) and (hover: hover)');
  const motionButton=el('button','station-motion','Pause previews');
  motionButton.type='button';motionButton.setAttribute('aria-pressed','false');
  grid.parentElement.querySelector('.start-sec-head').appendChild(motionButton);
  let paused=REDUCED;
  motionButton.textContent=paused?'Play previews':'Pause previews';
  motionButton.setAttribute('aria-pressed',String(paused));
  function sync(){
    cards.forEach((card,i)=>{
      const active=i===selected;
      card.classList.toggle('is-selected',active);
      let video=videos.get(i);
      if(active && !paused && previewInView && !document.hidden && MODES[i].video && !video){
        video=document.createElement('video');
        video.muted=true;video.defaultMuted=true;video.loop=true;video.playsInline=true;
        video.preload='none';video.setAttribute('aria-hidden','true');
        video.src=STATIC+MODES[i].video;
        video.addEventListener('playing',()=>{if(!disposed)card.classList.add('has-preview');});
        card.querySelector('.station-fig').appendChild(video);videos.set(i,video);
      }
      if(video){
        if(active&&!paused&&previewInView&&!document.hidden)video.play().catch(()=>{});
        else video.pause();
      }
    });
    grid.classList.toggle('previews-paused',paused);
  }
  const select=i=>{if(disposed)return;selected=i;sync();};
  const listeners=[];
  cards.forEach((card,i)=>{
    const enter=()=>{if(wide.matches){clearTimeout(hoveringTimer);hoveringTimer=setTimeout(()=>select(i),100);}};
    const leave=()=>clearTimeout(hoveringTimer);
    const focus=()=>select(i);
    card.addEventListener('pointerenter',enter);card.addEventListener('pointerleave',leave);card.addEventListener('focus',focus);
    listeners.push(()=>{card.removeEventListener('pointerenter',enter);card.removeEventListener('pointerleave',leave);card.removeEventListener('focus',focus);});
  });
  motionButton.addEventListener('click',()=>{
    paused=!paused;motionButton.textContent=paused?'Play previews':'Pause previews';
    motionButton.setAttribute('aria-pressed',String(paused));sync();
  });
  document.addEventListener('visibilitychange',sync);
  const visibility=new Map();
  const observer=new IntersectionObserver(entries=>{
    if(disposed)return;
    entries.forEach(entry=>visibility.set(cards.indexOf(entry.target),entry.intersectionRatio));
    const visible=[...visibility].sort((a,b)=>b[1]-a[1]);
    previewInView=!!visible.length&&visible[0][1]>.08;
    if(!wide.matches&&previewInView)selected=visible[0][0];
    sync();
  },{threshold:[0,.1,.4,.65]});
  cards.forEach(card=>observer.observe(card));
  sync();
  return ()=>{
    disposed=true;observer.disconnect();clearTimeout(hoveringTimer);document.removeEventListener('visibilitychange',sync);
    listeners.forEach(remove=>remove());
    videos.forEach(video=>{video.pause();video.removeAttribute('src');video.load();});
  };
}

function sectionLabel(title) {
  const h = el("div", "start-sec-head");
  h.appendChild(el("h2", "", esc(title)));
  return h;
}

function stationCard(m) {
  const a = el("a", "station");
  // A LINK HAS TO WORK WHEN IT IS THE ONLY THING YOU HAVE. This was "#machine=<id>", which the
  // boot reads only as a carousel POSITION for the showroom - so opening it cold, reloading it,
  // bookmarking it or middle-clicking it landed on the picker with no station, no heading and
  // no dock. That was the URL shown on hover and copied by "copy link address" for all five
  // cards. Four of the stations have a real route of their own; the two that live on "/" get
  // #screen=, which the boot does open onto the station.
  const route = routeFor(m.mode);
  a.href = route !== "/" ? route : "/#screen=" + m.id;
  a.setAttribute("aria-label", m.name + " - takes " + m.input + ", gives " + m.output);
  const fig = el("div", "station-fig");
  if (m.poster) {
    const img = el("img");
    img.src = "/static/" + m.poster;
    img.alt = "";
    img.loading = "lazy";
    img.decoding = "async";
    fig.appendChild(img);
  }
  a.appendChild(fig);
  const body = el("div", "station-body");
  // "Station" is already said by the section header above; saying it five more times is what
  // made the row read as a marketing grid rather than a set of machines.
  body.appendChild(el("b", "", esc((m.title && m.title[0]) || m.name)));
  // Two lines on every card by construction, so it cannot wrap on some and not others.
  const flow = el("div", "station-flow");
  flow.appendChild(el("span", "in", esc(m.input)));
  flow.appendChild(el("span", "out", esc(m.output)));
  body.appendChild(flow);
  a.appendChild(body);
  a.addEventListener("click", e => { e.preventDefault(); openStudioMode(m); });
  return a;
}

function paintDeck(strip) {
  strip.innerHTML = "";
  const job = (OTHER_RUNNING || [])[0];
  strip.dataset.jobId=String(job?.id || "");
  if (!job) {
    // One bit of information, stated once. The dot is the state; the word "IDLE" and a sentence
    // saying the same thing were two more encodings of it. The room this frees goes to the one
    // fact somebody opening a machine that renders for twenty minutes actually wants.
    strip.classList.remove("is-live");
    strip.appendChild(el("span", "deck-state", "Idle"));
    const last = el("span", "deck-doing", "");
    last.id = "deck-last";
    strip.appendChild(last);
    return;
  }
  strip.classList.add("is-live");
  strip.appendChild(el("span", "deck-state", "Running"));
  const line = el("div", "deck-line");
  line.appendChild(el("b", "deck-name", esc(job.title || job.slug || "Job " + job.id)));
  line.appendChild(el("span", "deck-doing", "Reading the machine"));
  const bar = el("div", "deck-bar");
  bar.appendChild(el("i", ""));
  line.appendChild(bar);
  strip.appendChild(line);
  strip.appendChild(btn("Open", () => {
    S.jobId = job.id; S.view = "wait"; S.jobStatus = "running"; render();
  }, "primary"));
  jget("/job-status?id=" + encodeURIComponent(job.id) + "&v3=1").then(d => {
    if (!strip.isConnected) return;
    const v3 = (d && d.v3) || d || {};
    const doing = strip.querySelector(".deck-doing");
    const beats = v3.beats || [];
    const at = typeof v3.beat_index === "number" ? v3.beat_index : -1;
    const mins = Math.max(0, Math.round((v3.elapsed || 0) / 60));
    let text = v3.phase || v3.activity || "Working";
    if (beats.length && at >= 0) text = "Beat " + (at + 1) + " of " + beats.length + " \u00b7 " + text;
    if (doing) doing.textContent = text + " \u00b7 " + mins + " min in";
    const fill = strip.querySelector(".deck-bar i");
    const frac = typeof v3.progress === "number" ? v3.progress
               : (beats.length && at >= 0 ? (at + 1) / beats.length : 0);
    if (fill) fill.style.width = Math.max(2, Math.min(100, frac * 100)) + "%";
  }).catch(() => {});
}

const STATION_OF = {"": "Clip Station", clip: "Clip Station", ai: "AI Video Station",
  longform: "Sketch Station", physics: "Physics Bench", enhance: "Effects Station",
  reddit: "Story Station", challenge: "Challenge Station", dreamcore: "AI Video Station"};

async function fillContinue(box) {
  let d = null;
  try { d = await jget("/projects-list"); } catch (e) {}
  if (!box.isConnected) return;
  const all = ((d && d.projects) || []).filter(r => !r.hidden);
  // the idle strip's second half: the one fact worth knowing on open
  const lastLine = document.getElementById("deck-last");
  if (lastLine) {
    const done = all.find(r => r.has_video && r.edited);
    lastLine.textContent = done
      ? "Last finished " + String(done.edited).slice(5, 16).replace("-", "/")
      : "Nothing yet";
  }
  const rows = all.slice(0, 4);
  box.innerHTML = "";
  if (!rows.length) {
    const empty = el("div", "cont-empty");
    empty.appendChild(el("b", "", "Nothing here yet"));
    empty.appendChild(el("span", "", "Finish a short and it will wait for you here."));
    box.appendChild(empty);
    return;
  }
  rows.forEach(row => {
    const a2 = el("a", "cont-card");
    a2.href = "#";
    const fig = el("div", "cont-fig");
    fig.setAttribute("data-letter", (STATION_OF[row.kind || ""] || "C").charAt(0));
    if (row.thumb_url) {
      const i = el("img");
      i.src = row.thumb_url; i.alt = ""; i.loading = "lazy";
      fig.appendChild(i);
    }
    a2.appendChild(fig);
    const t = el("div", "cont-body");
    t.appendChild(el("b", "", esc(row.title || row.slug || "Untitled")));
    const meta = el("span", "cont-meta");
    const bits = [STATION_OF[row.kind || ""] || "Clip Station"];
    if (row.edited) bits.push(String(row.edited).slice(5, 16).replace("-", "/"));
    if (row.running) bits.push("running");
    else if (row.failed) bits.push("needs a rerun");
    meta.textContent = bits.join(" \u00b7 ");
    t.appendChild(meta);
    a2.appendChild(t);
    a2.addEventListener("click", e => { e.preventDefault(); loadProject(row.slug); });
    box.appendChild(a2);
  });
  // the fifth column is the way out of the four, so the row lands on the same grid as the
  // stations instead of being four wider cards on a grid of its own
  const more = el("a", "cont-more");
  more.href = "#";
  more.appendChild(el("b", "", "All projects"));
  more.appendChild(el("span", "", all.length + " in the Library"));
  more.addEventListener("click", e => { e.preventDefault(); goAssets(); });
  box.appendChild(more);
}

function renderShowroom(main) {
  if (SHOWROOM_3D) { renderRetroShowroom(main); return; }
  renderStartScreen(main);
}

function renderStudioHome(main) {
  setBackdrop(null);
  const studio = el("div", "studio-home");
  studio.innerHTML = `<div class="studio-eyebrow"><span class="studio-live"></span> YOUR CREATIVE STUDIO <span>IDEA → FINAL CUT</span></div>
    <section class="studio-intro"><h1>Video production.<br><em>Five workstations.</em></h1><div class="studio-intro-note"><span class="studio-cross" aria-hidden="true">✳</span><p>Edit footage, generate shots, illustrate scripts,<br>render simulations or finish an existing cut.</p></div></section>`;
  if (pendingDraft) studio.appendChild(restoreBanner());
  const feature = el("section", "studio-feature");
  feature.setAttribute("aria-label", "Create with real footage");
  feature.innerHTML = `<div class="studio-feature-copy"><span class="studio-kicker">01 / CLIP STUDIO</span><h2>The world is<br>your footage.</h2><p>Turn your script into a story told with real moments. Sourced, voiced, cut and captioned.</p><div class="studio-feature-actions"></div><div class="studio-tags"><span>REAL FOOTAGE</span><span>AI DIRECTION</span><span>9:16</span></div></div><div class="studio-film"><img src="${STATIC + MODES[0].poster}" alt="Example of a Short created from real footage"><div class="film-top"><span>SHORTSLAB ORIGINAL</span><span>9:16 / SHORT FILM</span></div><div class="film-caption"><span>FROM SCRIPT TO SCREEN</span><b>Every frame.<br>A new perspective.</b></div><div class="film-time"><span>● PREVIEW</span><span>00:00 — 00:20</span></div></div>`;
  feature.querySelector('.studio-feature-actions').appendChild(btn('Create a Short <span aria-hidden="true">↗</span>', () => openStudioMode(MODES[0]), 'primary studio-cta'));
  const movie = feature.querySelector('.studio-film');
  const play = btn(svgIcon('play'), () => {
    const existing = movie.querySelector('video');
    if (existing) { existing.pause(); existing.remove(); movie.classList.remove("is-playing"); play.innerHTML = svgIcon('play'); play.setAttribute('aria-label','Play example'); return; }
    const video = document.createElement('video'); video.src = STATIC + MODES[0].video; video.muted = true; video.loop = true; video.playsInline = true; video.controls = true; video.setAttribute("aria-label", "Clip Studio example");
    movie.appendChild(video); movie.classList.add("is-playing"); video.play().catch(() => { video.remove(); movie.classList.remove("is-playing"); play.innerHTML = svgIcon("play"); play.setAttribute("aria-label", "Play example"); toast("The preview could not play. Try again.", true); });
    play.textContent = 'Ⅱ'; play.setAttribute('aria-label','Pause example');
  }, 'studio-play');
  play.setAttribute('aria-label','Play example'); movie.appendChild(play);
  studio.appendChild(feature);
  const section = el('div','studio-section-title','<h2>Find your creative direction.</h2><span>THE TOOLKIT / 04</span>'); studio.appendChild(section);
  const grid = el('div','studio-tools');
  const names = ['AI cinema','Sketch stories','Physics lab','The finishing room'];
  const notes = ['Imagine it. Direct every shot.','Big ideas, beautifully explained.','Perfectly timed. Deeply satisfying.','Sound, captions and the final polish.'];
  MODES.slice(1).forEach((m,i) => {
    const card = el('button','studio-tool'); card.type = 'button';
    card.innerHTML = `<div class="studio-tool-image"><img loading="lazy" src="${STATIC + m.poster}" alt=""><span class="studio-tool-no">0${i+2}</span><span class="studio-tool-arrow" aria-hidden="true">↗</span></div><div class="studio-tool-copy"><h3>${names[i]}</h3><p>${notes[i]}</p></div>`;
    card.addEventListener('click',()=>openStudioMode(m)); grid.appendChild(card);
  });
  studio.appendChild(grid);
  const foot = el('footer','studio-footer','<span>SHORTSLAB — MADE FOR THE NEXT FRAME.</span>');
  const library = el('a','','Open your library ↗'); library.href='/assets'; library.addEventListener('click',e=>{e.preventDefault();goAssets();}); foot.appendChild(library);
  studio.appendChild(foot); main.appendChild(studio);
}
boot();
