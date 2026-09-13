/* Shortslab chat shell - deterministic UI state machine.
 *
 * HARD RULES (migration spec):
 * - No AI model is ever called to decide a message, step or transition. Everything below is
 *   template text + explicit switch/step logic.
 * - Every submission maps 1:1 onto the EXISTING backend routes with the same field names as
 *   the legacy forms (BOOT.manifest documents the contract; test_chat_ui.py enforces parity).
 * - Messages are derived from persisted state (renderAll is idempotent) so reload/polling
 *   never duplicates a message or progress card.
 */
"use strict";

const BOOT = JSON.parse(document.getElementById("chat-boot").textContent);
const T = BOOT.strings;
const OPT = BOOT.options;
const MAN = BOOT.manifest;
const REASONING = BOOT.reasoningConfig || {};
const SEED_TTS_MODEL = "bytedance/seed-speech-tts-2.0";
const isSeedTts = (model) => String(model || "") === SEED_TTS_MODEL ||
  ["seed-speech", "seed-speech-tts-2.0"].includes(String(model || ""));
// Which voices this model can ACTUALLY produce. isSeedTts() reads the alias name, and the
// Gemini aliases resolve to the ByteDance model server-side (Google's own TTS endpoints accept
// a job and then fail it, every time). Offering Gemini narrator names for a Seed job meant all
// thirty of them failed the Seed voice check and fell back to stokie_en - the same voice no
// matter what the user picked. OPT.tts_provider is the server's own resolution.
const ttsProviderOf = (model) => {
  const map = OPT.tts_provider || {};
  const key = String(model || "");
  return map[key] || (isSeedTts(model) ? "seed" : "gemini");
};
const ttsVoiceOptions = (model) => ({
  seed: OPT.tts_voice_seed || [],
  inworld: OPT.tts_voice_inworld || [],
}[ttsProviderOf(model)] || OPT.tts_voice_gemini || OPT.tts_voice || []);
function resetTtsVoice(holder) {
  const choices = ttsVoiceOptions(holder.tts_model);
  if (!choices.some(o => o.value === holder.tts_voice))
    holder.tts_voice = { seed: "stokie_en", inworld: "Dennis" }[ttsProviderOf(holder.tts_model)]
      || (choices[0] || {}).value || "";
}
function previewTts(holder, mode) {
  const a = ensureAudio();
  a.src = BOOT.voices_preview + encodeURIComponent(holder.tts_voice || "") +
    "&model=" + encodeURIComponent(holder.tts_model || "pro") + (mode ? "&mode=" + encodeURIComponent(mode) : "");
  a.play().catch(() => {});
}
function seedTtsSettings(holder) {
  if (ttsProviderOf(holder.tts_model) !== "seed") return null;
  const box = el("div", "seed-tts-settings");
  box.appendChild(el("div", "out-sec-hint", "Seed Speech controls the voice directly. Gemini narrator names and Gemini delivery presets are not used."));
  const instruction = el("textarea"); instruction.rows = 2;
  instruction.placeholder = "Optional delivery direction, e.g. warm, intimate, energetic";
  instruction.value = holder.tts_voice_instruction || "";
  instruction.addEventListener("input", () => { holder.tts_voice_instruction = instruction.value; persist(); });
  box.appendChild(instruction);
  const grid = el("div", "seed-tts-grid");
  const select = (label, options, value, onChange) => {
    const f = el("label", "seed-tts-field"); f.appendChild(el("span", "", label));
    const s = el("select"); options.forEach(o => s.appendChild(new Option(o.label || o, o.value || o)));
    s.value = value == null ? "" : String(value); s.addEventListener("change", () => { onChange(s.value); persist(); });
    f.appendChild(s); return f;
  };
  const number = (label, key, min, max, step, fallback) => {
    const f = el("label", "seed-tts-field"); f.appendChild(el("span", "", label));
    const input = el("input"); input.type = "number"; input.min = min; input.max = max; input.step = step;
    input.value = holder[key] == null || holder[key] === "" ? fallback : holder[key];
    input.addEventListener("input", () => { holder[key] = input.value; persist(); }); f.appendChild(input); return f;
  };
  grid.appendChild(select("Language", OPT.seed_tts_languages || [], holder.tts_language || "", v => holder.tts_language = v));
  grid.appendChild(number("Speed", "tts_native_speed", "0.5", "2", "0.1", "1"));
  grid.appendChild(number("Volume", "tts_volume", "0.5", "2", "0.1", "1"));
  grid.appendChild(number("Pitch", "tts_pitch", "-12", "12", "1", "0"));
  grid.appendChild(select("Sample rate", [8000, 16000, 22050, 24000, 32000, 44100, 48000].map(v => ({value:String(v), label:v + " Hz"})), holder.tts_sample_rate || "24000", v => holder.tts_sample_rate = v));
  grid.appendChild(select("Output", [{value:"mp3",label:"MP3"},{value:"opus",label:"Opus"}], holder.tts_output_format || "mp3", v => holder.tts_output_format = v));
  box.appendChild(grid); return box;
}

function reasoningOptions(modelId, previous) {
  const cfg = REASONING[modelId];
  if (!cfg) return { options: [], value: "" };
  const valid = (cfg.options || []).some(o => o.value === previous);
  return { options: cfg.options || [], value: valid ? previous : cfg.defaultValue };
}
function appendReasoningField(card, modelId, holder) {
  const rm = reasoningOptions(modelId, holder.reasoning_mode);
  if (!rm.options.length) { holder.reasoning_mode = ""; return; }
  holder.reasoning_mode = rm.value;
  const f = selectField("Reasoning mode", rm.options, rm.value, v => holder.reasoning_mode = v);
  f.style.marginTop = "22px";     // breathing room below the model choice buttons
  card.appendChild(f);
}

/* ------------------------------------------------------------------ tiny dom helpers */
const $ = (id) => document.getElementById(id);
const el = (tag, cls, html) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (html !== undefined) n.innerHTML = html;
  return n;
};
const esc = (s) => String(s == null ? "" : s)
  .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
const fmtDate = (s) => s || "";
// A marked hook belongs to the script it was marked in, but "is it still there?" must be asked
// of the WORDS. Asked of the raw characters, a retyped space or a markdown hash unmarked the
// hook without saying so, and the Google intro then typed the script's opening line instead.
const hookWords = (s) => String(s == null ? "" : s).replace(/#/g, " ")
  .replace(/\s+/g, " ").trim().toLowerCase();

async function jget(url) { const r = await fetch(url); return r.json(); }

// What a project's `kind` is CALLED on screen. The raw key doubles as a CSS class, so it stays
// lowercase; only the badge text lives here. An ordinary generated/scraped project has no kind
// and so no badge.
const KIND_LABEL = { sfx: "SFX", asmr: "ASMR", vfx: "VFX", longform: "Sketch", motion_loop: "Loop",
                     dreamcore: "Dreamcore" };
function kindLabel(kind) { return KIND_LABEL[kind] || ""; }
// STATE is separate from KIND: a failed Sketch used to show only "failed" and lose its
// category. State sits top-right, kind top-left; both can show at once.
const STATE_LABEL = { running: "Running", failed: "Failed", editing: "In edit",
                      awaiting: "Needs clips" };
function projState(p) {
  if (p.running) return "running";
  // Waiting for the user to bring generated clips back is the normal middle of a dreamcore
  // project, and it can last days. Before this it read as "Failed", because the only test
  // was "has no render".
  if (p.awaiting) return "awaiting";
  if (p.failed) return "failed";
  if (p.has_timeline && !p.has_video) return "editing";   // clip short living in the timeline
  return "";
}
/* single-frame project thumbnail (hook/opening) + kind badge (top-left) + state badge (top-right) */
function projThumb(p, cls) {
  const kind = p.kind || p.preview_kind || "";
  const state = projState(p);
  const kindTag = kindLabel(kind)
    ? `<span class="pv-tag pv-tag-${kind}">${kindLabel(kind)}</span>` : "";
  const stateTag = state
    ? `<span class="pv-tag pv-state pv-tag-${state}">${state === "running" ? '<i class="pv-dot"></i>' : ""}${STATE_LABEL[state]}</span>` : "";
  const inner = p.thumb_url
    ? `<img loading="lazy" src="${esc(p.thumb_url)}" alt="">`
    : (p.video_url ? `<video muted preload="none" src="${esc(p.video_url)}"></video>` : "");
  const borders = [kind ? "pv-" + kind : "", state ? "pv-" + state : ""].filter(Boolean).join(" ");
  return `<span class="pv-wrap ${cls || ""} ${borders}">${inner}${kindTag}${stateTag}</span>`;
}
async function jpost(url, data) {
  const r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data || {}) });
  try { return await r.json(); } catch (e) { return { ok: r.ok }; }
}

/* ------------------------------------------------------------------ state */
const DEFAULT_VALUES = () => {
  // start from the SAME persisted defaults the legacy form used (ui_state.json)
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
  if (st.add_visual_effects === undefined) v.add_visual_effects = true;   // arrows on by default
  if (st.out_captions === undefined) v.out_captions = true;        // captions ON by default
  if (st.out_sfx === undefined) v.out_sfx = true;                  // sound effects ON by default
  if (st.out_transition_sfx === undefined) v.out_transition_sfx = true;
  if (st.halt_after_speech === undefined) v.halt_after_speech = false;
  if (!v.pipeline_version) v.pipeline_version = "v0.2";   // version chooser removed; always v0.2
  // Mini Story is retired; never restore it from legacy ui_state.json.
  v.clip_short_format = "standard";
  v.script_token_limit = "";
  if (!v.speaker_name) v.speaker_name = "Narrator";
  if (st.motion_loop_seamless === undefined) v.motion_loop_seamless = false;
  v.motion_loop_unlimited = true;
  // A new creation must start with a blank story. The legacy UI state also
  // stores the last submitted script, but that is not a reusable default.
  v.script = "";
  v.gen_topic = "";
  v.hook_text = "";
  v.impact_word = "";
  v.loaded_project_mode = v.loaded_project_mode || "normal";
  v.loaded_project_source = "";
  return v;
};

let S = {
  flow: null,              // script | reddit | longform | sfx | visual | captions | project
  step: "mode",           // current step id
  values: DEFAULT_VALUES(),
  master: {},              // master-tool settings (reasoning_model, sfx_amount, vfx_amount, caption_*)
  longform: {},            // model/aspect/concurrency
  topic: "",
  stories: [], story: null,
  completed: [],           // ordered list of completed step ids
  jobId: null,
  jobStatus: "",
  projectSlug: "",         // loaded project
  projectTitle: "",
  view: "chat",           // chat | assets
  showHidden: false,
  draft: true,
};
let FILES = {};             // step -> File (not persisted across reload)
let pollTimer = null, jobsTimer = null, saveTimer = null, scrapePreviewTimer = null;
let lastProgressHTML = "", lastMediaHTML = "", lastOutputsHTML = "", announcedPhases = [];
let prototypeMode = false;
/* True only when this document was asked for by name (?ui=chat / classic / v2). Every other way
   of arriving here is a leftover route, and its way back must lead to the showroom. */
function isClassicConsole() {
  try { return /^(chat|classic|v2)$/i.test(new URLSearchParams(location.search).get("ui") || ""); } catch (e) { return false; }
}

function persist() {
  clearTimeout(saveTimer);
  saveTimer = setTimeout(() => {
    const copy = { ...S };
    jpost("/chat-state", copy).catch(() => {});
  }, 350);
}

/* ------------------------------------------------------------------ flow definitions */
const FLOW_STEPS = {
  script: ["script", "source", "reasoning", "outputs", "review"],
  reddit: ["discover", "pick"],
  physics: ["scene", "shot"],
  lowpoly: ["setup"],
  longform: ["script", "settings"],
  sfx: ["upload", "settings"],
  visual: ["upload", "settings"],
  captions: ["upload", "settings"],
  asmr: ["upload", "settings"],
  enhance: ["choose"],
  actionedit: ["actionclips"],
  aishort: ["choose"],
  aicore: ["brief", "pick", "clips"],
  dreamcore: ["brief", "clips"],
  project: ["summary"],
};
function isCultureFacts() { return S.flow === "script" && !!S.values.culture_facts_mode; }
function isVisualsFromScript() { return S.flow === "script" && !!S.values.visuals_from_script_mode; }
function isDiscovery() { return isCultureFacts() && S.values.clip_short_format === "discovery"; }
function isOthersVsKing() { return isCultureFacts() && S.values.clip_short_format === "others_vs_king"; }
function estimatedScriptTokens(text) {
  const chunks = String(text || "").trim().match(/[\p{L}\p{N}]+|[^\s\p{L}\p{N}]/gu) || [];
  return chunks.length;
}
function applyCultureFactsPreset() {
  S.values.culture_facts_mode = true;
  S.values.clip_source = "scrape";
  // A DEFAULT, not a reset. renderSourceCard calls this on every render, so assigning here
  // unconditionally would silently undo the engine the user just picked.
  // Clip Short V4 is deliberately isolated: it uses Bright discovery and Bright
  // Web Unlocker media delivery only. Do not resurrect a legacy V2/V3 path from
  // persisted UI state.
  S.values.scraping_engine = "v4";
  S.values.enable_speaker_hook = false;
  S.values.speaker_image_path = "";
  FILES.speaker_image_file = null;
  S.values.out_web_images = false;
  S.values.out_wikimedia = false;
  S.values.out_gpt_images = false;
  S.values.out_video_clips = true;
  if (S.values.out_captions === undefined || S.values.out_captions === false) S.values.out_captions = true;
  // Clip Short has one deliberately narrow audio profile. Never carry the general
  // content/reaction-SFX preference from AI Short into this mode.
  S.values.out_sfx = false;
  if (S.values.out_transition_sfx === undefined) S.values.out_transition_sfx = true;
  if (!S.values.script_relevancy) S.values.script_relevancy = "90";
  if (isDiscovery()) S.values.halt_after_speech = false;   // discovery has no speech gate
}
function stepsFor(flow) {
  if (flow === "script" && isCultureFacts()) {
    if (isOthersVsKing()) return ["format", "script", "reasoning", "review"];
    return ["format", "script", "source", "reasoning", "outputs", "review"];
  }
  return FLOW_STEPS[flow] || [];
}
function stepIndex(step) { return stepsFor(S.flow).indexOf(step); }

function goto(step) { S.step = step; S.draft = true; renderAll(); persist(); }
function completeStep(step, next) {
  if (!S.completed.includes(step)) S.completed.push(step);
  goto(next);
}
function editStep(step) {
  // invalidate ONLY dependent later steps; earlier answers stay
  const order = stepsFor(S.flow);
  const idx = order.indexOf(step);
  S.completed = S.completed.filter(s => order.indexOf(s) < idx);
  goto(step);
}
function resetToMode() {
  stopPolling();
  S = { ...S, flow: null, step: "mode", values: DEFAULT_VALUES(), master: {}, longform: {},
        topic: "", stories: [], story: null, completed: [], jobId: null, jobStatus: "",
        projectSlug: "", projectTitle: "", view: "chat", draft: true };
  FILES = {}; lastProgressHTML = lastMediaHTML = lastOutputsHTML = ""; announcedPhases = [];
  renderAll(); persist();
}

/* ------------------------------------------------------------------ chat rendering (idempotent) */
const chat = $("chat");
function msgA(html, id) {
  const m = el("div", "msg assistant");
  if (id) m.dataset.mid = id;
  m.appendChild(el("div", "who", "Shortslab"));
  m.appendChild(el("div", "bubble", html));
  chat.appendChild(m); return m;
}
function msgU(html, editable) {
  const m = el("div", "msg user");
  const b = el("div", "bubble", html);
  if (editable) {
    const e = el("button", "edit-link", T.edit);
    e.addEventListener("click", () => editStep(editable));
    b.appendChild(e);
  }
  m.appendChild(b); chat.appendChild(m); return m;
}
function card(cls) { const c = el("div", "chat-card " + (cls || "")); chat.appendChild(c); return c; }
function scrollDown() { const sc = $("chat-scroll"); sc.scrollTop = sc.scrollHeight; }

/* one full deterministic re-render from state (no duplicates possible) */
function renderAll() {
  stopPolling();
  chat.innerHTML = "";
  const app = $("app");
  // Sidebar selection follows application state on every render. Previously it was mostly set
  // by the clicked nav button, so returning from Assets through an in-page Back action left the
  // Assets button visually selected over the New Creation screen.
  markNav(S.view === "assets" ? "assets" : "new");
  app.classList.toggle("proto-home", prototypeMode && S.view !== "assets" && !S.flow && !S.jobId);
  app.classList.toggle("proto-flow", prototypeMode && S.view !== "assets" && (!!S.flow || !!S.jobId));
  app.classList.toggle("proto-assets", prototypeMode && S.view === "assets");
  app.classList.toggle("proto-job", prototypeMode && S.view !== "assets" && !!S.jobId);
  renderTopbar();
  if (S.view === "assets") { renderAssetsView(); setComposer("off"); scrollDown(); return; }
  if (!S.flow) {
    // an active job renders even without a flow (e.g. reattached via deep link)
    if (S.jobId) { renderJobSection(); scrollDown(); return; }
    if (prototypeMode) {
      renderPrototypeHome();
      $("chat-scroll").scrollTop = 0;
    } else {
      renderModeMenu(); scrollDown();
    }
    setComposer("off"); return;
  }

  // A RUNNING JOB REPLACES ITS OWN SETTINGS. The config steps were drawn first and the job
  // section appended under them, so every screen a run produces - the live log, the thumbnail
  // choices, the pre-render timeline - arrived with the whole settings form still sitting above
  // it. Nothing there can be changed once the run has started, so it is only in the way.
  // The prototype shell already did this; it was never applied to the default one.
  if (S.jobId) {
    // The composer belonged to the step that started the run (often an upload field). Nothing
    // is typed or dropped into a job, and every job-adjacent path already turns it off.
    setComposer("off");
    renderJobSection(); scrollDown(); return;
  }

  ({ script: renderScriptFlow, reddit: renderRedditFlow,
     physics: renderPhysicsFlow, lowpoly: renderLowpolyFlow,
     longform: renderLongformFlow, sfx: () => renderMasterFlow("sfx"),
     visual: () => renderMasterFlow("visual"), captions: () => renderMasterFlow("captions"),
     asmr: () => renderMasterFlow("asmr"),
     enhance: renderEnhanceFlow,
     actionedit: renderActionEditFlow,
     aishort: renderAIShortPicker,
     aicore: renderAICoreFlow,
     dreamcore: renderDreamcoreFlow,
     project: renderProjectFlow }[S.flow] || renderModeMenu)();

  if (prototypeMode && !S.jobId) decoratePrototypeFlow();
  if (S.jobId) renderJobSection();
  // Design V2 config steps are single workspace surfaces, not chat transcripts. Landing on a
  // new step at the bottom clipped its title on shorter displays; jobs still follow the live log.
  if (prototypeMode && !S.jobId) $("chat-scroll").scrollTop = 0;
  else scrollDown();
}

// Short chip labels for the step carousel (one per known step id)
const STEP_SHORT = {
  script: "Story", voice: "Voice", source: "Footage", reasoning: "Director",
  outputs: "Finish", review: "Review", topic: "Topic", discover: "Discover",
  pick: "Select", upload: "Source", settings: "Settings", choose: "Upgrade",
  summary: "Project", hook: "Opening", version: "Edit",
  concept: "Concept", motion: "Motion",
};
let _lastStepperOffset = null; // px offset of the previous bar, so the next slides from it
let _stepperRO = null;
// A horizontal, auto-centering step carousel: done steps sit to the left
// (clickable to jump back), the active step is centered, upcoming steps trail
// off to the right (dimmed). buildStepper only builds the DOM — centering is
// done by centerActiveStepper against the LIVE DOM so re-render races can't
// leave a stale element un-positioned.
function buildStepper(flowSteps, activeStep) {
  if (!flowSteps || flowSteps.length < 2) return null;
  const wrap = el("nav", "proto-stepper"); wrap.setAttribute("aria-label", "Steps");
  const track = el("div", "proto-stepper-track");
  const activeIdx = Math.max(0, flowSteps.indexOf(activeStep));
  flowSteps.forEach((st, i) => {
    const chip = el("button", "proto-step-chip"); chip.type = "button";
    chip.innerHTML = `<span class="psc-dot"></span><span class="psc-label">${esc(STEP_SHORT[st] || st)}</span>`;
    chip.classList.add(i < activeIdx ? "done" : (i === activeIdx ? "active" : "upcoming"));
    chip.setAttribute("aria-current", i === activeIdx ? "step" : "false");
    if (i < activeIdx) chip.addEventListener("click", () => editStep(st));
    else chip.disabled = true; // active + upcoming are not jump targets
    track.appendChild(chip);
  });
  wrap.appendChild(track);
  return wrap;
}
// Center the active chip of whatever stepper is currently in the DOM. The final
// transform is set synchronously (so it is correct even in a background tab where
// rAF is paused); the slide from the previous bar's offset is layered on via the
// Web Animations API when `animate` is set and we have a previous position.
function centerActiveStepper(animate) {
  const wrap = document.querySelector(".proto-active-card .proto-stepper");
  if (!wrap) return true; // nothing to place (single-step flow) — stop retrying
  const track = wrap.querySelector(".proto-stepper-track");
  const active = track && track.querySelector(".proto-step-chip.active");
  if (!track || !active || !wrap.clientWidth) return false; // not laid out yet
  const reduce = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const target = wrap.clientWidth / 2 - (active.offsetLeft + active.offsetWidth / 2);
  track.style.transition = "none";
  track.style.transform = `translateX(${target}px)`; // authoritative final state
  if (animate && !reduce && _lastStepperOffset != null && _lastStepperOffset !== target && track.animate) {
    try {
      track.animate(
        [{ transform: `translateX(${_lastStepperOffset}px)` }, { transform: `translateX(${target}px)` }],
        { duration: 420, easing: "cubic-bezier(.16,1,.3,1)" });
    } catch (e) {}
  }
  _lastStepperOffset = target;
  return true;
}
// Kick off placement after layout; retry with timers (which, unlike rAF, still
// fire in a hidden tab) until the wrap has a width, then keep it centered on resize.
function activateStepper() {
  let tries = 0;
  const kick = () => { if (!centerActiveStepper(true) && tries++ < 40) setTimeout(kick, 16); };
  kick();
  if (window.ResizeObserver) {
    if (_stepperRO) _stepperRO.disconnect();
    const wrap = document.querySelector(".proto-active-card .proto-stepper");
    if (wrap) {
      _stepperRO = new ResizeObserver(() => {
        if (!wrap.isConnected) { _stepperRO.disconnect(); _stepperRO = null; return; }
        centerActiveStepper(false);
      });
      _stepperRO.observe(wrap);
    }
  }
}
// Per-step header copy [KICKER, TITLE, SUBTITLE], keyed by step id.
function isLongform() { return S.flow === "longform"; }
function stepCopy(step) {
  return ({
    // `script` and `settings` are shared keys, so their copy has to bend to the flow that is
    // using them - longform cannot generate a script, and its settings step is about narration,
    // not the "enhancement intensity" the SFX/Visual/Caption masters set there.
    script: isLongform()
    ? ["SCRIPT", "Paste your script", "The full narration, start to finish - it sets the length of the video."]
    : isDiscovery()
      ? ["DISCOVERY", "Set the direction", "Optional topic + narrator - the agent finds the material and writes the script."]
      : ["STORY", "Add your script", "Paste it or generate a fresh one."],
    format:["FORMAT", "Choose the shape of the short", "Start with a fact-led edit, agent discovery, or an action-and-payoff format."],
    hook:["OPENING", "Mark the hook", "Select the line that must stop the scroll."],
    version:["EDIT", "Choose the cutting style", "Use the current edit system or switch to classic."],
    source:["FOOTAGE", isCultureFacts() ? "Tune the footage search" : "Choose visual models",
            isCultureFacts() ? "Set relevance, ranking and optional search terms." : "Pick how the visuals should be generated."],
    reasoning:["DIRECTOR", "Choose the AI director", "Select the model that plans the production."],
    voice:["VOICE", "Choose the narrator", "Pick a voice and preview the delivery."],
    outputs:["FINISH", "Select the final layers", "Choose sound, captions and approval behavior."],
    review:["REVIEW", "Ready to create", "Check the essentials and start the production."],
    topic:["TOPIC", "Define the idea", "Give the agent one clear direction."],
    discover:["DISCOVER", "Find the story", "Choose where the search should begin."],
    pick:["SELECT", "Choose the strongest story", "Pick the version worth producing."],
    upload:["SOURCE", "Add your video", "Drop in the cut you want to enhance."],
    settings: isLongform()
      ? ["PRODUCTION", "Set up the narration", "Pick the narrator, the models and how the voiceover gets approved."]
      : ["SETTINGS", "Direct the enhancement", "Choose the intensity and model."],
    choose: S.flow === "aishort"
      ? ["CREATE", "Choose an AI workflow", "Start with narration, a connected visual world, or a music-led dream edit."]
      : ["UPGRADE", "Choose an enhancement", "Pick one focused production pass."],
    concept:["CONCEPT", "Build the world", "Describe one surreal place, subject or point of view."],
    motion:["MOTION", "Direct the loop", "Choose the camera path and intensity; the coded pass keeps it fluid."],
    summary:["PROJECT", "Project overview", "Choose the next action."],
    brief:["WORLD", "Name the place", "One or two lines. The aesthetic is the subject; this is only where it happens."],
    clips:["CLIPS", "Generate, then drop them back in", "Copy each prompt into your generator and bring the clips here. The project waits for you."],
    actionclips:["FOOTAGE", "Add the three clips of one Short",
                 "In order. They are cut on the action, graded and mixed into one finished vertical short."],
    scene:["SIMULATION", "Choose what gets simulated", "Every one is a real Blender scene, solved not animated."],
    shot:["THE SHOT", "Set it up", "Materials, counts, masses, camera - the numbers the scene was built to accept."],
  }[step] || ["SETTINGS", "Configure this step", "Make your choices and continue."]);
}
let _lastStepIdx = null;   // for slide direction between steps
function decoratePrototypeFlow() {
  chat.querySelectorAll(":scope > .msg").forEach(m => m.classList.add("proto-flow-context"));
  const cards = chat.querySelectorAll(":scope > .chat-card");
  if (!cards.length) return;
  const active = cards[cards.length - 1];
  // the step class alone is ambiguous: `script` and `settings` are shared by several flows whose
  // steps hold different things, so the surface carries its flow too and CSS can tell them apart
  active.classList.add("proto-active-card", "proto-step-surface", `proto-step-${S.step || "default"}`,
                       `proto-mode-${S.flow || "default"}`);
  active.dataset.step = S.step || "default";
  active.dataset.mode = S.flow || "default";
  const copy = stepCopy(S.step);
  const flowSteps = stepsFor(S.flow);
  const stepIndex = Math.max(0, flowSteps.indexOf(S.step));
  const prev = stepIndex > 0 ? flowSteps[stepIndex - 1] : null;
  const next = stepIndex < flowSteps.length - 1 ? flowSteps[stepIndex + 1] : null;
  // header: title only (no back button, no step chips) + a quiet progress count
  const head = el("header", "proto-config-head");
  head.innerHTML = `<div class="proto-config-title"><small>${esc(copy[0])}</small>` +
    `<span class="proto-config-count">${String(stepIndex + 1).padStart(2,"0")} / ${String(flowSteps.length).padStart(2,"0")}</span>` +
    `<h1>${esc(copy[1])}</h1><p>${esc(copy[2])}</p></div>`;
  active.insertBefore(head, active.firstChild);
  // Unified Back button lives in the FOOTER, on the LEFT, level with Continue on the right.
  // `margin-right:auto` on it pushes every other footer control to the right regardless of
  // how that step laid its footer out, which also fixes steps whose Continue sat on the left.
  const foots = active.querySelectorAll(".card-foot");
  let foot = foots[foots.length - 1];
  // every step gets a footer with a Back button (create one for steps that had none, e.g. Story Flow)
  if (!foot) { foot = el("div", "card-foot"); active.appendChild(foot); }
  {
    foot.querySelectorAll(".spacer").forEach(s => s.remove());
    foot.querySelectorAll(".btn").forEach(b => {
      if (b.textContent.trim().toLowerCase() === String(T.back || "Back").trim().toLowerCase()) b.remove();
    });
    const back = el("button", "btn proto-foot-back", `${protoIcon("arrow")}<span>${prev ? (T.back || "Back") : "All modes"}</span>`);
    back.type = "button";
    back.addEventListener("click", () => prev ? editStep(prev) : resetToMode());
    foot.insertBefore(back, foot.firstChild);
  }
  // Whole-box transition: on a step change the ENTIRE menu card slides in from the side it
  // came from (forward = from the right, back = from the left), with faint neighbour peeks.
  const dir = (_lastStepIdx == null) ? 0 : Math.sign(stepIndex - _lastStepIdx);
  _lastStepIdx = stepIndex;
  active.classList.toggle("has-prev", !!prev);
  active.classList.toggle("has-next", !!next);
  const reduce = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  if (!reduce && dir !== 0 && active.animate) {
    try {
      active.animate(
        [{ transform: `translateX(${dir * 46}px)`, opacity: 0 }, { transform: "translateX(0)", opacity: 1 }],
        { duration: 380, easing: "cubic-bezier(.16,1,.3,1)" });   // slow-out settle
    } catch (e) {}
  }
  renderStepGhosts(active, prev, next);
}
// #177 - carousel coverflow: the previous/next step menus are rendered smaller + dimmed, peeking
// on the left/right of the active card. Lightweight (title only) so they never run a step's real
// logic; gated to wide windows so they can't cause horizontal scroll.
function renderStepGhosts(active, prev, next) {
  const col = active.parentElement;
  if (!col) return;
  col.querySelectorAll(".proto-ghost").forEach(g => g.remove());
  const reduce = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  if (reduce || (!prev && !next)) return;
  try {
    if (getComputedStyle(col).position === "static") col.style.position = "relative";
    col.style.overflowX = "clip";   // ghosts extend far off-card; never let them add horizontal scroll
  } catch (e) {}
  const mk = (step, side) => {
    if (!step) return null;
    const cp = stepCopy(step);
    const g = el("div", "proto-ghost proto-ghost-" + side);
    g.setAttribute("aria-hidden", "true");
    g.innerHTML = `<span class="pg-kicker">${esc(cp[0])}</span><span class="pg-title">${esc(cp[1])}</span><span class="pg-sub">${esc(cp[2])}</span>`;
    col.appendChild(g);
    return g;
  };
  const gp = mk(prev, "prev"), gn = mk(next, "next");
  const place = () => {
    const cardW = active.offsetWidth, cardH = active.offsetHeight;
    if (!cardW) return false;
    // only show the flanking menus when there's real room beside the card (else no peek)
    if (col.clientWidth < cardW + 150) { if (gp) gp.style.display = "none"; if (gn) gn.style.display = "none"; return true; }
    const cardLeft = active.offsetLeft, cardTop = active.offsetTop;
    const gw = Math.round(cardW * 0.84), gh = Math.round(cardH * 0.9), peek = 46;
    [gp, gn].forEach(g => { if (!g) return; g.style.display = ""; g.style.width = gw + "px"; g.style.height = gh + "px"; g.style.top = (cardTop + (cardH - gh) / 2) + "px"; });
    if (gp) gp.style.left = (cardLeft - gw + peek) + "px";
    if (gn) gn.style.left = (cardLeft + cardW - peek) + "px";
    return true;
  };
  if (!place()) { let n = 0; const t = () => { if (!place() && n++ < 40) setTimeout(t, 16); }; setTimeout(t, 16); }
  if (window.ResizeObserver) {
    if (_ghostRO) _ghostRO.disconnect();
    _ghostRO = new ResizeObserver(() => { if (!active.isConnected) { _ghostRO.disconnect(); _ghostRO = null; return; } place(); });
    _ghostRO.observe(col);
  }
}
let _ghostRO = null;

/* ------------------------------------------------------------------ topbar */
function renderTopbar() {
  // Minimal bar: only a loaded-project title and job status/cancel. Otherwise it stays
  // invisible (the drawer button still floats on narrow windows).
  const ctx = $("tb-ctx"); const act = $("tb-actions");
  ctx.innerHTML = ""; act.innerHTML = "";
  let hasContent = false;
  if (S.projectTitle) { ctx.appendChild(el("b", "", esc(S.projectTitle))); hasContent = true; }
  if (S.jobStatus) {
    const b = el("span", "tb-badge" + (S.jobStatus === "running" ? " run" : ""), esc(S.jobStatus));
    ctx.appendChild(b); hasContent = true;
  }
  if (S.jobId && ["running", "cancelling", "awaiting_approval"].includes(S.jobStatus)) {
    const b = el("button", "btn small danger", T.cancel_process);
    b.addEventListener("click", cancelJob); act.appendChild(b); hasContent = true;
  }
  $("topbar").classList.toggle("empty", !hasContent);
}

/* ------------------------------------------------------------------ mode menu (empty state) */
const MODES = [
  { id: "culture", grp: 0, ico: "🌏", t: "Clip Short", d: "Turn facts, strange stories and fascinating topics into a short using real sourced footage from TikTok, Instagram and X matching your script." },
  { id: "visualscript", grp: 0, ico: "✦", t: "AI Short", d: "Create a short using AI-generated visuals and scraped web imagery matching the script." },
  { id: "script", grp: 0, ico: "🎬", t: T.mode_script_t, d: T.mode_script_d },
  // Physics sits where the Reddit story mode used to be (user 2026-07-26). The
  // reddit flow itself is untouched and still reachable at /reddit.
  { id: "physics", grp: 0, ico: "⚙", t: T.mode_physics_t, d: T.mode_physics_d },
  { id: "longform", grp: 0, ico: "🎨", t: T.mode_longform_t, d: T.mode_longform_d },
  { id: "sfx", grp: 1, ico: "🔊", t: T.mode_sfx_t, d: T.mode_sfx_d },
  { id: "visual", grp: 1, ico: "➜", t: T.mode_vfx_t, d: T.mode_vfx_d },
  { id: "captions", grp: 1, ico: "💬️", t: T.mode_captions_t, d: T.mode_captions_d },
];
function renderModeMenu() {
  msgA(esc(T.what_creating));
  [T.grp_create, T.grp_masters].forEach((cap, g) => {
    const c = card();
    c.appendChild(el("div", "card-cap", esc(cap)));
    const grid = el("div", "mode-grid");
    const visibleModes = MODES.filter(m => m.grp === g && !["sfx", "visual", "captions"].includes(m.id));
    if (g === 1) visibleModes.unshift({ id:"enhance", ico:"✦", t:"Enhance video", d:"Polish sound, visuals, captions or original ambience in an existing video." });
    visibleModes.forEach(m => {
      const b = el("button", "mode-card");
      b.innerHTML = `<span class="mh"><span class="mi">${m.ico}</span>${esc(m.t)}</span><span class="md">${esc(m.d)}</span>`;
      b.addEventListener("click", () => selectMode(m.id));
      grid.appendChild(b);
    });
    c.appendChild(grid);
  });
}

/* ------------------------------------------------------------------ opt-in Creator Launchpad prototype
   This is a second presentation layer over the existing deterministic flow state. It never
   invents a route or duplicates a backend operation: every action below calls the same
   selectMode/showAssets/loadProject/startJob functions as the classic shell. */
function protoIcon(name) {
  const paths = {
    spark: '<path d="M12 3l1.4 4.1L17.5 8.5l-4.1 1.4L12 14l-1.4-4.1-4.1-1.4 4.1-1.4L12 3Z"/><path d="M5 14l.8 2.2L8 17l-2.2.8L5 20l-.8-2.2L2 17l2.2-.8L5 14Z"/>',
    sound: '<path d="M11 5 6 9H3v6h3l5 4V5Z"/><path d="M15 9a5 5 0 0 1 0 6"/><path d="M18 6a9 9 0 0 1 0 12"/>',
    visual: '<path d="M3 7V4h3M18 4h3v3M21 17v3h-3M6 20H3v-3"/><circle cx="12" cy="12" r="3"/><path d="M5 12s2.5-4 7-4 7 4 7 4-2.5 4-7 4-7-4-7-4Z"/>',
    captions: '<path d="M4 5h16v12H8l-4 3V5Z"/><path d="M8 10h8M8 13h5"/>',
    flask: '<path d="M9 3h6M10 3v5l-5 9a2 2 0 0 0 1.8 3h10.4A2 2 0 0 0 19 17l-5-9V3"/><path d="M7.5 15h9"/>',
    reddit: '<circle cx="12" cy="12" r="8"/><circle cx="9" cy="12" r="1"/><circle cx="15" cy="12" r="1"/><path d="M8 15c2 1.5 6 1.5 8 0M14 4l1-2 3 1"/>',
    longform: '<path d="M4 5h16v14H4z"/><path d="M8 9h8M8 13h8M8 17h5"/>',
    arrow: '<path d="M5 12h14M14 7l5 5-5 5"/>',
    folder: '<path d="M3 6h7l2 2h9v10H3V6Z"/>',
    activity: '<path d="M3 12h4l2-6 4 12 2-6h6"/>',
    more: '<circle cx="5" cy="12" r="1"/><circle cx="12" cy="12" r="1"/><circle cx="19" cy="12" r="1"/>',
  };
  return `<svg class="proto-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${paths[name] || paths.spark}</svg>`;
}
function renderPrototypeHome() {
  const home = el("div", "proto-launchpad");
  home.innerHTML = `
    <header class="proto-hero">
      <span class="proto-hero-kicker">NEW PROJECT</span>
      <h1>Make it<br><em>move.</em></h1>
    </header>
    <section class="proto-work-section" aria-labelledby="proto-create-title">
      <header class="proto-section-heading proto-create-heading"><div><h2 id="proto-create-title">Create</h2></div></header>
      <div class="proto-primary-tools" aria-label="Production tools">
      <button type="button" class="proto-tool proto-creator-card proto-tool-culture" data-mode="culture">
        <video id="proto-culture-preview" muted autoplay loop playsinline preload="auto" poster="/file?path=static%2Fpreviews%2Fclip_short_trash_poster.jpg" src="/file?path=static%2Fpreviews%2Fclip_short_trash_20s_hd.mp4" aria-hidden="true"></video>
        <span class="proto-culture-shade"></span>
        <span class="proto-tool-icon">${protoIcon("activity")}</span>
        <span class="proto-tool-copy"><small>SCRAPE V2 · COMPLETE SHORT</small><strong>Culture Facts</strong>
          <em>Script → relevance-first TikTok, X and Instagram footage → voice, sound and captions.</em></span>
        <span class="proto-tool-go">Create Culture Facts ${protoIcon("arrow")}</span>
      </button>
      <button type="button" class="proto-tool proto-creator-card proto-tool-main" data-mode="script">
        <span class="proto-tool-icon">${protoIcon("spark")}</span>
        <span class="proto-tool-copy"><small>START FROM AN IDEA</small><strong>Create a short</strong>
          <em>Script, voice, footage and sound — built as one directed production.</em></span>
        <span class="proto-tool-go">Start creating ${protoIcon("arrow")}</span><i class="proto-orbit"></i>
      </button>
      <button type="button" class="proto-tool proto-creator-card proto-tool-visualscript" data-mode="visualscript">
        <video muted autoplay loop playsinline preload="auto" poster="/file?path=static%2Fpreviews%2Fai_short_motion_poster.jpg" src="/file?path=static%2Fpreviews%2Fai_short_pig_war_motion_20s_hd.mp4" aria-hidden="true"></video>
        <span class="proto-culture-shade"></span>
        <span class="proto-tool-icon">${protoIcon("spark")}</span>
        <span class="proto-tool-copy"><small>NO SCRAPING · GENERATIVE MEDIA</small><strong>Visuals from Script</strong>
          <em>Direct a complete short with AI video, AI images, web images and Wikimedia footage.</em></span>
        <span class="proto-tool-go">Build from script ${protoIcon("arrow")}</span>
      </button>
      <button type="button" class="proto-tool proto-creator-card proto-tool-physics" data-mode="physics">
        <video muted autoplay loop playsinline preload="auto" poster="/file?path=static%2Fpreviews%2Fphysics_sweep_poster.jpg" src="/file?path=static%2Fpreviews%2Fphysics_sweep.mp4" aria-hidden="true"></video>
        <span class="proto-culture-shade"></span>
        <span class="proto-tool-copy"><small>SIMULATED IN BLENDER</small><strong>Physics Sweep</strong>
          <em>One 3D scene, one value swept, with ASMR impact sound on the frame it hits.</em></span>
        <span class="proto-tool-go">Set up a simulation ${protoIcon("arrow")}</span>
      </button>
      </div>
    </section>
    <section class="proto-secondary-tools" aria-label="More creation modes">
      <button type="button" data-mode="enhance">${protoIcon("visual")}<span><b>Enhance video</b><small>SFX, visual effects or captions</small></span></button>
      <button type="button" data-mode="longform">${protoIcon("longform")}<span><b>Longform Visuals</b><small>Illustrate longer narration</small></span></button>
      <button type="button" data-mode="lowpoly">${protoIcon("flask")}<span><b>Low Poly Story</b><small>Crude 3D story from a prompt</small></span></button>
    </section>
    <section class="proto-home-section">
      <header><div><small>CONTINUE WORKING</small><h2>Recent projects</h2></div><button type="button" data-open-assets>View all ${protoIcon("arrow")}</button></header>
      <div class="proto-project-grid" id="proto-project-grid"><div class="proto-skeleton"></div><div class="proto-skeleton"></div><div class="proto-skeleton"></div></div>
    </section>
    <section class="proto-live-dock" id="proto-live-dock" hidden aria-live="polite"></section>`;
  chat.appendChild(home);
  // Prototype creation menu: keep the four distinct production modes only.
  home.querySelector('[data-mode="script"]')?.remove();
  const labelCard = (mode, eyebrow, title, description, action) => {
    const node = home.querySelector(`[data-mode="${mode}"]`);
    if (!node) return node;
    const small = node.querySelector(".proto-tool-copy small"); if (small) small.textContent = eyebrow;
    const strong = node.querySelector(".proto-tool-copy strong"); if (strong) strong.textContent = title;
    const em = node.querySelector(".proto-tool-copy em"); if (em) em.textContent = description;
    const go = node.querySelector(".proto-tool-go"); if (go && action) go.innerHTML = `${esc(action)} ${protoIcon("arrow")}`;
    return node;
  };
  labelCard("culture", "REAL FOOTAGE SHORT", "Clip Short",
    "Turn facts, strange stories and fascinating topics into a short using real sourced footage from TikTok, Instagram and X matching your script.", "Create Clip Short");
  const aiShort = home.querySelector('[data-mode="visualscript"]');
  labelCard("visualscript", "AI VISUAL SHORT", "AI Short",
    "Create a short using AI-generated visuals and scraped web imagery matching the script.", "Build AI Short");
  labelCard("physics", "SIMULATED IN BLENDER", "Physics Sweep",
    "Choose a hand-built Blender scene - dropped, crushed, rolled, magnetised - set its "
    + "materials and masses, and let the solver do the rest. ASMR impact sound lands on "
    + "the frame it hits.", "Choose a simulation");
  const longform = home.querySelector('[data-mode="longform"]');
  if (longform) {
    longform.className = "proto-tool proto-creator-card proto-tool-longform";
    longform.innerHTML = `<span class="proto-sketch-preview sketch-originals" aria-hidden="true">
      <img class="sketch-original original-1" src="/file?path=static%2Fpreviews%2Fsketch_longform%2Fscene_04.jpg" alt="">
      <img class="sketch-original original-2" src="/file?path=static%2Fpreviews%2Fsketch_longform%2Fscene_05.jpg" alt="">
      <img class="sketch-original original-3" src="/file?path=static%2Fpreviews%2Fsketch_longform%2Fscene_01.png" alt="">
      <img class="sketch-original original-4" src="/file?path=static%2Fpreviews%2Fsketch_longform%2Fscene_09.jpeg" alt="">
      <b class="sketch-motion motion-a">→</b><b class="sketch-motion motion-b">✦</b><b class="sketch-motion motion-c">···</b></span>
      <span class="proto-sketch-shade"></span><span class="proto-tool-icon">${protoIcon("longform")}</span>
      <span class="proto-tool-copy"><small>STICKMAN LONGFORM</small><strong>Sketch Explainer</strong>
      <em>Create long-form narrated videos with AI generated stickman sketch visuals.</em></span>
      <span class="proto-tool-go">Create Sketch Explainer ${protoIcon("arrow")}</span>`;
    const primary = home.querySelector(".proto-primary-tools");
    const physics = home.querySelector('[data-mode="physics"]');
    if (primary) primary.insertBefore(longform, physics || null);
  }
  const lowpoly = home.querySelector('[data-mode="lowpoly"]');
  if (lowpoly) {
    lowpoly.className = "proto-tool proto-creator-card proto-tool-lowpoly";
    lowpoly.innerHTML = `<span class="proto-lowpoly-preview" aria-hidden="true"><i></i><i></i><i></i><b></b></span>
      <span class="proto-lowpoly-shade"></span><span class="proto-tool-copy"><strong>Low Poly Story</strong>
      <em>Turn a prompt into a stylized, deliberately crude 3D story.</em></span>`;
    home.querySelector(".proto-primary-tools")?.appendChild(lowpoly);
  }
  const enhance = home.querySelector('[data-mode="enhance"]');
  if (enhance) {
    const upgradeSection = enhance.parentElement;
    if (upgradeSection) {
      upgradeSection.className = "proto-upgrade-section";
      const upgradeHead = el("header", "proto-section-heading");
      upgradeHead.innerHTML = `<h2>Upgrade</h2>`;
      upgradeSection.insertBefore(upgradeHead, enhance);
    }
    enhance.className = "proto-enhance-showcase";
    enhance.innerHTML = `<span class="proto-enhance-preview">
      <span class="proto-vfx-demo" aria-hidden="true"><i class="vfx-focus"><b></b><b></b><b></b><b></b></i>
      <i class="vfx-arrow">➜</i><i class="vfx-arrow vfx-arrow-two">➜</i><i class="vfx-ring"></i>
      <i class="vfx-speed-lines"><b></b><b></b><b></b></i><strong class="vfx-pop-label">LOOK HERE</strong>
      ${Array.from({length:10}, (_, i) => `<b class="vfx-particle p-${i + 1}"></b>`).join("")}</span>
      <span class="proto-caption-demo" aria-hidden="true"><b style="--word:0">MAKE</b><b style="--word:1">EVERY</b><b style="--word:2">SECOND</b><b class="hot" style="--word:3">HIT</b></span>
      <i class="proto-sfx-wave" aria-hidden="true">${Array.from({length:18}, (_, i) => `<b style="--i:${i}"></b>`).join("")}</i>
      </span>
      <span class="proto-enhance-copy"><small>POLISH AN EXISTING CUT</small><strong>Enhance video</strong>
      <em>Add cinematic sound effects, animated captions and attention-guiding visual effects to an existing video.</em><span>Enhance a video ${protoIcon("arrow")}</span></span>`;
    // Timeline Editor entry — sits right next to "Enhance video" with an animated editor preview.
    if (upgradeSection) {
      const tl = document.createElement("button");
      tl.type = "button";
      tl.className = "proto-enhance-showcase proto-timeline-showcase";
      tl.innerHTML = `<span class="proto-enhance-preview proto-tl-preview" aria-hidden="true">
        <span class="tl-demo-track tl-demo-clips"><i class="tl-demo-clip"></i><i class="tl-demo-clip"></i><i class="tl-demo-clip"></i><i class="tl-demo-clip"></i></span>
        <span class="tl-demo-track tl-demo-fx"><i class="tl-demo-fxdot"></i><i class="tl-demo-fxdot"></i><i class="tl-demo-fxdot"></i></span>
        <i class="tl-demo-playhead"></i></span>
        <span class="proto-enhance-copy"><small>FRAME-ACCURATE EDITOR</small><strong>Timeline Editor</strong>
        <em>Fine-tune clips, sound effects and visuals on a CapCut-style timeline.</em><span>Open editor ${protoIcon("arrow")}</span></span>`;
      tl.addEventListener("click", () => openTimelineNav());
      upgradeSection.appendChild(tl);
    }
  }
  home.querySelectorAll(".proto-primary-tools .proto-tool-icon").forEach(icon => icon.remove());
  home.querySelectorAll(".proto-tool-copy small,.proto-enhance-copy small").forEach(label => label.remove());
  home.querySelectorAll(".proto-primary-tools .proto-tool-go,.proto-enhance-copy > span").forEach(action => action.remove());
  home.querySelectorAll(".proto-tool-copy em,.proto-enhance-copy em").forEach(description => description.remove());
  home.querySelectorAll(".proto-primary-tools .proto-tool").forEach((card, index) => {
    card.dataset.cardIndex = String(index + 1).padStart(2, "0");
  });
  const openAssets = home.querySelector("[data-open-assets]");
  home.querySelector(".proto-home-section")?.remove();
  home.querySelectorAll("video").forEach(video => {
    video.muted = true; video.defaultMuted = true;
    const start = () => video.play().catch(() => {});
    if (video.readyState >= 2) start(); else video.addEventListener("loadeddata", start, { once:true });
  });
  home.querySelectorAll("[data-mode]").forEach(b => {
    b.addEventListener("click", () => enterModeFromCard(b, b.dataset.mode));
    if (b.classList.contains("proto-tool")) {
      b.addEventListener("pointermove", e => {
        if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
        const r = b.getBoundingClientRect();
        b.style.setProperty("--mx", ((e.clientX - r.left) / r.width * 100).toFixed(1) + "%");
        b.style.setProperty("--my", ((e.clientY - r.top) / r.height * 100).toFixed(1) + "%");
        b.style.setProperty("--tx", ((e.clientX - r.left - r.width / 2) * .018).toFixed(2) + "px");
        b.style.setProperty("--ty", ((e.clientY - r.top - r.height / 2) * .018).toFixed(2) + "px");
      });
      b.addEventListener("pointerleave", () => { b.style.removeProperty("--tx"); b.style.removeProperty("--ty"); });
    }
  });
  if (openAssets) openAssets.addEventListener("click", () => showAssets(false));
  refreshPrototypeHomeData();
}
function enterModeFromCard(source, mode) {
  if (!source || document.body.classList.contains("mode-entering") ||
      window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
    selectMode(mode); return;
  }
  const rect = source.getBoundingClientRect();
  if (mode === "culture") prepareUiWhoosh();
  const clone = source.cloneNode(true);
  clone.removeAttribute("data-mode"); clone.classList.add("proto-mode-zoom");
  Object.assign(clone.style, {
    left:rect.left + "px", top:rect.top + "px", width:rect.width + "px", height:rect.height + "px"
  });
  document.body.classList.add("mode-entering");
  source.style.opacity = "0";
  document.body.appendChild(clone);
  clone.querySelectorAll("video").forEach(video => { video.muted = true; video.play().catch(() => {}); });
  requestAnimationFrame(() => requestAnimationFrame(() => clone.classList.add("expand")));
  // Clip Short begins revealing its first configuration surface during the final 160ms of
  // the card expansion. The expanding preview remains above it and fades away, producing one
  // continuous spatial transition instead of zoom -> blank beat -> sudden wizard.
  // Start revealing the config surface WHILE the preview is still zooming (not after it fully
  // stops), so the blur-dissolve exit flows into the incoming content instead of popping.
  // #7 - slower zoom, and the config menu flies in LATER (reveal delayed to match the 1000ms zoom)
  const revealAt = mode === "culture" ? 660 : 700;
  setTimeout(() => {
    selectMode(mode);
    document.body.classList.add("mode-arriving");
    if (mode === "culture") {
      document.body.classList.add("mode-arriving-clip");
      // Start audio on the same painted frame as the card flight. Starting it directly
      // after the DOM class change made the sound lead the visible motion by one frame.
      requestAnimationFrame(() => playUiWhoosh());
      // Keep the expanded footage visibly layered over the already-arriving script box. Only
      // after the overlap has registered does the preview complete its fade.
      requestAnimationFrame(() => clone.classList.add("ui-visible"));
      setTimeout(() => clone.classList.add("finish"), 640);
      // Pin the rendered card to the animation's exact end state before mode-arriving-clip is
      // removed. Without this handoff Chromium briefly rebuilt the layer on the CPU and flashed
      // the underlying unanimated card for one frame.
      setTimeout(() => {
        const card = chat.querySelector(":scope > .proto-active-card");
        if (card) card.classList.add("proto-flight-landed");
      }, 1120);
    } else {
      requestAnimationFrame(() => clone.classList.add("finish"));
    }
    setTimeout(() => {
      clone.remove(); source.style.opacity = "";
      document.body.classList.remove("mode-entering", "mode-arriving", "mode-arriving-clip");
    }, mode === "culture" ? 1380 : 900);
  }, revealAt);
}
async function refreshPrototypeHomeData() {
  if (!prototypeMode || !$("app").classList.contains("proto-home")) return;
  const grid = $("proto-project-grid");
  try {
    const d = await jget("/projects-list");
    if (grid) {
      grid.innerHTML = "";
      (d.projects || []).slice(0, 3).forEach((p, idx) => {
      const b = el("button", "proto-project");
      const fallback = `<span class="proto-poster-fallback proto-poster-${idx + 1}">${protoIcon("folder")}</span>`;
      const preview = p.thumb_url ? `<img loading="lazy" src="${esc(p.thumb_url)}" alt="">` : fallback;
      const status = p.running ? "Run in progress" : p.awaiting ? "Waiting for your clips"
        : p.failed ? "Needs attention" : p.has_timeline ? "Ready to edit" : "In progress";
      b.innerHTML = `<span class="proto-project-poster">${preview}${kindLabel(p.kind) ? `<i>${esc(kindLabel(p.kind))}</i>` : ""}</span>
        <span class="proto-project-copy"><b>${esc(p.title || p.slug)}</b><small>${esc(status)} · ${esc(fmtDate(p.edited))}</small></span>${protoIcon("more")}`;
      b.addEventListener("click", () => loadProject(p.slug, p));
        grid.appendChild(b);
      });
      if (!grid.children.length) grid.innerHTML = '<div class="proto-empty">Your first production will appear here.</div>';
    }
  } catch (e) { if (grid) grid.innerHTML = '<div class="proto-empty">Projects are temporarily unavailable.</div>'; }
  const dock = $("proto-live-dock");
  try {
    const d = await jget("/jobs-list");
    if (!dock) return;
    const active = (d.jobs || []).filter(j => ["running", "cancelling", "awaiting_approval"].includes(j.status));
    dock.hidden = !active.length;
    dock.innerHTML = "";
    active.slice(0, 2).forEach((j, idx) => {
      const b = el("button", "proto-live-job");
      const progress = Math.max(8, Math.min(94, Number(j.progress || j.percent || (idx ? 34 : 67))));
      b.innerHTML = `<span class="proto-live-thumb">${protoIcon("activity")}</span><span class="proto-live-copy"><small>LIVE · ${esc(j.kind || "PRODUCTION")}</small>
        <b>${esc(j.project_slug || "Active production")}</b><em>${esc(j.last_log || j.status)}</em><i><span style="width:${progress}%"></span></i></span>
        <strong>${progress}%</strong>${protoIcon("arrow")}`;
      b.addEventListener("click", () => startJob(j.id, j.kind)); dock.appendChild(b);
    });
  } catch (e) { if (dock) dock.hidden = true; }
}
function selectMode(id) {
  if (id === "visualscript") {
    S.flow = "aishort"; S.step = "choose"; S.completed = [];
    renderAll(); persist(); return;
  }
  if (id === "visualscript-classic") id = "visualscript";
  const culture = id === "culture";
  const visualScript = id === "visualscript";
  const motionLoop = id === "aicore";
  S.flow = (culture || visualScript) ? "script" : id; S.completed = [];
  S.values.culture_facts_mode = culture;
  S.values.visuals_from_script_mode = visualScript;
  S.values.motion_loop_mode = motionLoop;
  if (culture) {
    applyCultureFactsPreset();
    // Every new Clip Short is V4 (Scrape.do discovery). Old V2/V3 values in persisted
    // UI state are never allowed to switch this new run onto a fallback path.
    S.values.scraping_engine = "v4";
    S.values.tts_model = "pro";
    S.values.tts_voice = "Laomedeia";
    S.values.speaker_name = "Narrator";
    S.values.vision_model = "google/gemini-3.7-flash";
    S.values.clip_short_format = "standard";
    S.values.script_token_limit = "";
    S.values.script_relevancy = "90";
    S.values.out_sfx = false;
    S.values.out_transition_sfx = true;
  }
  else if (id === "script" || visualScript) {
    S.values.clip_source = "generate";
    S.values.scraping_engine = "v4";
  }
  S.step = stepsFor(S.flow)[0];
  renderAll(); persist();
}

/* ------------------------------------------------------------------ FLOW: A Video from a Script */
function renderScriptFlow() {
  const done = (s) => S.completed.includes(s);
  msgU(esc(isCultureFacts() ? "Clip Short" : T.mode_script_t));

  // Clip Short has a narrated fact format, autonomous Discovery, and a no-voice
  // action/payoff format inspired by "Others vs the king" social edits.
  if (isCultureFacts() && !done("format")) {
    msgA("Choose the Clip Short format.");
    const c = card("clip-format-card");
    const grid = el("div", "clip-format-grid");
    const choose = (value) => {
      S.values.clip_short_format = value;
      S.values.script_token_limit = "";
      completeStep("format", "script");
    };
    const standard = el("button", "clip-format-choice");
    standard.innerHTML = `<small>MULTI-BEAT</small><strong>Fact Short</strong><span>Several facts or angles, matched with broader real footage.</span><em>100–140 words</em>`;
    standard.addEventListener("click", () => choose("standard"));
    const disc = el("button", "clip-format-choice mini");
    disc.innerHTML = `<small>NO SCRIPT NEEDED</small><strong>Discovery</strong><span>The agent gathers real Japan-related material from multiple sources, writes the fact script, and builds the short around it.</span><em>Topic optional</em>`;
    disc.addEventListener("click", () => choose("discovery"));
    const others = el("button", "clip-format-choice others");
    others.innerHTML = `<small>ACTION + PAYOFF</small><strong>Others doing X</strong><span>Several people perform one action, followed by one unbelievable final execution.</span><em>TikTok + Instagram</em>`;
    others.addEventListener("click", () => choose("others_vs_king"));
    grid.appendChild(standard); grid.appendChild(disc); grid.appendChild(others); c.appendChild(grid);
    const foot = el("div", "card-foot");
    foot.appendChild(btn(T.back, resetToMode, "ghost")); c.appendChild(foot);
    setComposer("off"); return;
  }

  // step: script
  msgA(isOthersVsKing()
    ? "Choose the action for the comparison."
    : isDiscovery()
    ? "Set the direction for the Discovery agent."
    : esc(T.send_script) + `<div class="card-note">${esc(T.script_hint)}</div>`);
  if (done("script")) {
    const sc = S.values.script || "";
    msgU(esc(sc.length > 220 ? sc.slice(0, 220) + "…" : sc), "script");
  } else if (S.step === "script") {
    const c = card("script-config-card");
    if (isOthersVsKing()) {
      c.appendChild(el("div", "card-note",
        "Name one filmable action. The editor finds several recognizable attempts, then reserves the most astonishing matching clip for the final payoff."));
      const action = document.createElement("input");
      action.type = "text";
      action.placeholder = "Action (optional) — e.g. high jumping, skiing, diving, skateboarding";
      action.value = S.values.others_action || "";
      action.style.cssText = "width:100%; margin:8px 0 5px;";
      action.addEventListener("input", () => { S.values.others_action = action.value; persist(); });
      c.appendChild(action);
      c.appendChild(el("div", "card-note", "Leave it blank and the agent chooses a fresh, highly visual action automatically."));
    } else if (isDiscovery()) {
      c.appendChild(el("div", "card-note",
        "Discovery mode: enter a topic below, leave the script blank, and the agent gathers "
        + "real footage before writing a Japan-focused fact Short around it. Add "
        + "*instructions* after the topic to guide the agent, e.g. *use catchy and funny scenes*."));
      const ti = document.createElement("input");
      ti.type = "text";
      ti.placeholder = "Topic (optional) — e.g. Japanese vending machines *use funny scenes*";
      ti.style.cssText = "width:100%; margin:6px 0 4px;";
      ti.value = S.values.gen_topic || "";
      ti.addEventListener("input", () => { S.values.gen_topic = ti.value; persist(); });
      c.appendChild(ti);
      const originalAudio = document.createElement("label");
      originalAudio.style.cssText = "display:flex; align-items:center; gap:8px; margin:8px 0; cursor:pointer;";
      const originalInput = document.createElement("input");
      originalInput.type = "checkbox";
      originalInput.checked = !!S.values.discovery_keep_original_audio;
      originalInput.addEventListener("change", () => {
        S.values.discovery_keep_original_audio = originalInput.checked;
        persist();
      });
      originalAudio.appendChild(originalInput);
      originalAudio.appendChild(el("span", "", "Keep some of original audio"));
      c.appendChild(originalAudio);
      c.appendChild(el("div", "card-note",
        "Alternates explanatory voiceover with selected source moments. The narrator mutes while the original audio plays."));
      // CANDIDATE LIBRARY (user 2026-07-23): every candidate ever shown as a pick,
      // browsable here; "Use" pins the run to that exact video (no search, no gate).
      const libRow = el("div", "");
      libRow.style.cssText = "display:flex; align-items:center; gap:8px; margin:2px 0 6px; flex-wrap:wrap;";
      c.appendChild(libRow);
      const renderPinned = () => {
        libRow.innerHTML = "";
        libRow.appendChild(btn("Candidate library", () => openCandidateLibrary(c, renderPinned), "ghost small"));
        if (S.values.candidate_url) {
          const chip = el("span", "");
          chip.style.cssText = "font-size:11.5px; color:var(--p-green,#39ff14); border:1px solid var(--line-strong); border-radius:8px; padding:3px 8px; display:inline-flex; align-items:center; gap:6px;";
          chip.appendChild(el("span", "", "Using saved candidate"));
          const x = el("button", "", "×");
          x.type = "button";
          x.style.cssText = "border:0; background:transparent; color:inherit; cursor:pointer; font-size:14px; line-height:1; padding:0;";
          x.addEventListener("click", () => { S.values.candidate_url = ""; persist(); renderPinned(); });
          chip.appendChild(x);
          libRow.appendChild(chip);
        }
      };
      renderPinned();
    }
    const ta = el("textarea", "script-box"); ta.id = "script-edit";
    ta.placeholder = T.script_placeholder;
    ta.value = S.values.script || "";
    if (isDiscovery() || isOthersVsKing()) { ta.style.display = "none"; ta.value = ""; }
    c.appendChild(ta);
    const sizeScriptBox = () => {
      ta.style.height = "auto";
      // compact: grows with content but starts small and never dominates the card
      ta.style.height = Math.min(Math.max(150, ta.scrollHeight + 2), Math.round(window.innerHeight * .42)) + "px";
    };
    let tokenMeter = null;
    const paintTokenMeter = () => {
      if (!tokenMeter) return;
      const count = estimatedScriptTokens(ta.value);
      tokenMeter.textContent = `${count} / 130 estimated tokens`;
      tokenMeter.classList.toggle("over", count > 130);
    };
    ta.addEventListener("input", () => {
      // Keep the canonical state in sync while typing so a TTS/provider re-render
      // cannot replace the current script with the last persisted value.
      S.values.script = ta.value;
      sizeScriptBox(); paintTokenMeter(); persist();
    });
    requestAnimationFrame(sizeScriptBox);
    // Hook + impact-word are marked RIGHT HERE on the same script field (no separate step, so the
    // text can never desync). Select text in the box above, then Mark hook / Mark impact.
    // Discovery has no user script -> no hook marking, no Script Creator column.
    if (!isDiscovery() && !isOthersVsKing()) {
      let selRange = null;
      const capture = () => {
        if (ta.selectionStart != null && ta.selectionEnd > ta.selectionStart)
          selRange = [ta.selectionStart, ta.selectionEnd];
      };
      ["mouseup", "keyup", "select"].forEach(ev => ta.addEventListener(ev, capture));
      const selText = () => selRange ? ta.value.substring(selRange[0], selRange[1]).trim() : "";
      const status = el("span", "script-hookstatus");
      const paint = () => {
        const parts = [];
        parts.push(S.values.hook_text ? "★ " + T.hook_marked + ": " + S.values.hook_text.slice(0, 70) : T.no_hook);
        if (S.values.impact_word) parts.push("⚡ " + S.values.impact_word);
        status.textContent = parts.join("   ·   ");
        status.className = "script-hookstatus" + (S.values.hook_text ? " on" : "");
      };
      const bar = el("div", "script-hookbar");
      bar.appendChild(btn("★ " + T.mark_hook, () => {
        const s = selText(); if (s) { S.values.hook_text = s; persist(); paint(); } else ta.focus();
      }, "ghost small"));
      bar.appendChild(btn(T.use_first_line, () => {
        const first = (ta.value || "").split(/(?<=[.!?])\s+|\n/)[0] || "";
        S.values.hook_text = first.trim(); persist(); paint();
      }, "ghost small"));
      bar.appendChild(btn("⚡ " + T.mark_impact, () => {
        const w = (selText().split(/\s+/)[0] || "").replace(/[^\p{L}\p{N}'-]/gu, "");
        if (w) { S.values.impact_word = w; persist(); paint(); } else ta.focus();
      }, "ghost small"));
      bar.appendChild(btn(T.clear_hook, () => {
        S.values.hook_text = ""; S.values.impact_word = ""; persist(); paint();
      }, "ghost small"));
      bar.appendChild(status);
      c.appendChild(bar);
      paint();
    }
    // Script Creator: topic -> gemini writes a reference-style script into the field
    // (empty topic = the model picks its own viral topic). The generated script always stays
    // here for review/edit - the run never starts from this step, so a halt toggle is noise.
    if (!isDiscovery() && !isOthersVsKing()) {
      const row = el("div", "script-gen-row");
      const ti = document.createElement("input");
      ti.type = "text"; ti.placeholder = "Topic (optional)"; ti.style.flex = "1";
      ti.value = S.values.gen_topic || "";
      ti.addEventListener("input", () => { S.values.gen_topic = ti.value; persist(); });
      row.appendChild(ti);
      const instructionWrap = el("label", "generator-instructions");
      instructionWrap.innerHTML = `<span>DIRECTION <em>OPTIONAL</em></span>`;
      const instructionInput = el("textarea", "generator-instructions-input");
      instructionInput.rows = 3;
      instructionInput.placeholder = "Tone, structure, or angle…";
      instructionInput.value = S.values.script_generator_instructions || "";
      instructionInput.addEventListener("input", () => {
        S.values.script_generator_instructions = instructionInput.value;
        persist();
      });
      instructionWrap.appendChild(instructionInput);
      const gb = btn("✨ " + T.gen_script, async (ev) => {
        const b = ev.currentTarget; b.disabled = true; b.textContent = T.gen_script_busy;
        try {
          const r = await fetch("/generate-script", { method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              topic: ti.value.trim(),
              instructions: instructionInput.value.trim(),
              format_mode: "standard",
              token_limit: null,
              reasoning_model: S.values.reasoning_model || "google/gemini-3.7-flash",
              clip_source: S.values.clip_source || "scrape",
            }) });
          const d = await r.json();
          if (!d.ok) throw new Error(d.error || "no script");
          ta.value = d.script; S.values.script = d.script;
          paintTokenMeter();
          S.values.hook_keywords = JSON.stringify(d.hook_keywords || []);
          S.values.hook_text = ""; S.values.impact_word = "";   // a new script invalidates the old marks
          persist();
          msgA(esc(T.gen_script_done)); renderAll();
        } catch (e) { errorCard(T.gen_script_err, String(e)); }
        b.disabled = false; b.textContent = "✨ " + T.gen_script;
      }, "secondary");
      row.appendChild(gb);
      // Recent-scripts library: every previously GENERATED script + (separately) every script
      // previously USED in a project; the preview is always the script's first sentence.
      row.appendChild(btn("📚 " + T.recent_scripts, async () => {
        const old = c.querySelector(".recent-scripts");
        if (old) { old.remove(); return; }          // toggle closed
        let d = { generated: [], used: [] };
        try { d = await jget("/recent-scripts"); } catch (e) {}
        const box = el("div", "recent-scripts");
        const addGroup = (label, items, isGen) => {
          if (!items || !items.length) return;
          box.appendChild(el("div", "card-cap", esc(label)));
          items.forEach(it => {
            const r = el("button", "rs-row");
            r.innerHTML = `<b>${esc(isGen ? (it.topic || "Generated") : it.project)}</b>
              <span>${esc(it.preview)}</span>`;
            r.title = it.preview;
            r.addEventListener("click", () => {
              ta.value = it.script; S.values.script = it.script; paintTokenMeter(); persist(); box.remove();
            });
            box.appendChild(r);
          });
        };
        addGroup(T.recent_generated, d.generated, true);
        addGroup(T.recent_used, d.used, false);
        if (!box.children.length) box.appendChild(el("div", "card-note", "No scripts yet."));
        const genCol = c.querySelector(".script-gen-col");
        if (genCol) genCol.appendChild(box);
        else c.insertBefore(box, c.querySelector(".card-foot"));
      }, "ghost"));
      c.appendChild(row);
      c.appendChild(instructionWrap);
    }
    // narrator is picked RIGHT HERE (no separate voice step): voice + preview + TTS model on one row
    if (isCultureFacts()) applyCultureFactsPreset();
    if (!isOthersVsKing()) {
      const nrow = el("div", "script-narrator");
      nrow.appendChild(el("span", "sn-lbl", esc(T.tts_voice || "Narrator")));
      const vsel = el("select");
      resetTtsVoice(S.values);
      ttsVoiceOptions(S.values.tts_model).forEach(o => vsel.appendChild(new Option(o.label, o.value)));
      if (S.values.tts_voice && [...vsel.options].some(o => o.value === S.values.tts_voice)) vsel.value = S.values.tts_voice;
      S.values.tts_voice = vsel.value;
      vsel.addEventListener("change", () => { S.values.tts_voice = vsel.value; persist(); });
      nrow.appendChild(vsel);
      nrow.appendChild(btn('<svg viewBox="0 0 24 24" width="13" height="13" fill="currentColor" aria-hidden="true" style="vertical-align:-2px"><path d="M8 5v14l11-7z"/></svg> Preview', () => {
        previewTts(S.values);
      }, "ghost small"));
      const msel = el("select");
      (OPT.tts_model || []).forEach(o => msel.appendChild(new Option(o.label, o.value)));
      if (S.values.tts_model && [...msel.options].some(o => o.value === S.values.tts_model)) msel.value = S.values.tts_model;
      S.values.tts_model = msel.value;
      msel.addEventListener("change", () => { S.values.tts_model = msel.value; resetTtsVoice(S.values); persist(); renderAll(); });
      nrow.appendChild(msel);
      c.appendChild(nrow);
      const seedSettings = seedTtsSettings(S.values); if (seedSettings) c.appendChild(seedSettings);
      // Region: drives the scrape search language/framing (japan = the original JP-first search).
      // A <japan>-style tag typed in the script still wins server-side; these chips just make the
      // choice visible. Default: japan for the culture-facts preset, general otherwise.
      // Discovery is Asia-locked -> region chips are noise there (languages row stays).
      {
        // STICKY across chats. The app always opens a fresh chat, so S.values is empty every
        // time and the region silently fell back to "general" on every single run - a
        // Japan-only channel then lost its Japan hook terms and proof terms unless the user
        // re-picked the chip or typed <japan> into the script. Last pick wins, japan is the
        // fallback because that is what this channel makes.
        if (!S.values.region) {
          let remembered = "";
          try { remembered = localStorage.getItem("sl-region") || ""; } catch (e) {}
          S.values.region = remembered
            || (isCultureFacts() ? "japan" : (remembered || "japan"));
        }
        if (!isDiscovery()) {
          const rrow = el("div", "script-region");
          rrow.appendChild(el("span", "sn-lbl", "Region"));
          const REGIONS = [["japan", "🇯🇵 Japan"], ["general", "🌍 General"],
                           ["switzerland", "🇨🇭 Switzerland"], ["history", "🏛 History"]];
          REGIONS.forEach(([val, label]) => {
            const b = btn(label, () => {
              S.values.region = val; persist();
              try { localStorage.setItem("sl-region", val); } catch (e) {}
              rrow.querySelectorAll("button").forEach(x => x.classList.toggle(
                "region-on", x.dataset.region === val));
            }, "ghost small");
            b.dataset.region = val;
            if (S.values.region === val) b.classList.add("region-on");
            rrow.appendChild(b);
          });
          c.appendChild(rrow);
        }
        // Search languages: one checkbox per language (replaces the old single
        // "multi-language search" toggle). Defaults follow the picked region.
        {
          const lrow = el("div", "script-region");
          lrow.appendChild(el("span", "sn-lbl", "Search languages"));
          const LANGS = [["ja", "🇯🇵 Japanese"], ["en", "🇬🇧 English"], ["zh", "🇨🇳 Chinese"],
                         ["ko", "🇰🇷 Korean"], ["es", "🇪🇸 Spanish"], ["de", "🇩🇪 German"]];
          const regionDefaults = { japan: "ja,en", general: "en", switzerland: "de,en", history: "en" };
          const selected = () => String(S.values.search_languages
            || regionDefaults[S.values.region] || "en").split(",").filter(Boolean);
          LANGS.forEach(([code, label]) => {
            const ml = document.createElement("label");
            ml.className = "region-multilang";
            const cb = document.createElement("input");
            cb.type = "checkbox"; cb.dataset.lang = code;
            cb.checked = selected().includes(code);
            cb.addEventListener("change", () => {
              let cur = selected().filter(x => x !== code);
              if (cb.checked) cur.push(code);
              if (!cur.length) { cur = [code]; cb.checked = true; }   // at least one language
              S.values.search_languages = cur.join(",");
              persist();
            });
            ml.appendChild(cb);
            ml.appendChild(document.createTextNode(" " + label));
            lrow.appendChild(ml);
          });
          c.appendChild(lrow);
        }
      }
      // optional talking-head speaker hook (non-scrape only) - a compact toggle; gallery on demand
      if (!isCultureFacts()) {
        const tog = toggleField(T.speaker_video, S.values.enable_speaker_hook, v => {
          S.values.enable_speaker_hook = v; renderAll(); persist();
        });
        tog.classList.add("script-spk-toggle");
        c.appendChild(tog);
        if (S.values.enable_speaker_hook) {
          const grid = el("div", "spk-grid"); grid.id = "spk-grid"; c.appendChild(grid);
          loadSpeakerGallery(grid);
          const up = btn("⬆ " + T.upload_image, () => pickFile("image/*", f => {
            FILES.speaker_image_file = f; S.values.speaker_image_path = "";
            const note = el("div", "upl-file"); note.innerHTML = `<span class="nm">${esc(f.name)}</span>`;
            grid.parentElement.insertBefore(note, grid.nextSibling);
          }), "ghost small");
          up.style.marginTop = "8px"; c.appendChild(up);
        }
      }
    }
    const foot = el("div", "card-foot");
    if (isCultureFacts()) foot.appendChild(btn(T.back, () => editStep("format"), "ghost"));
    foot.appendChild(el("span", "spacer"));
    foot.appendChild(btn(T.continue, () => {
      const v = ta.value.trim();
      const hasFactDiscoveryTopic = isCultureFacts() && !isDiscovery()
        && String(S.values.gen_topic || "").trim();
      if (!v && !isDiscovery() && !isOthersVsKing() && !hasFactDiscoveryTopic) { ta.focus(); return; }
      S.values.script = v;
      // Compare words, not bytes: retyping a space or a heading hash inside the script silently
      // unmarked the hook, and the Google intro then typed the script's opening sentence
      // instead of the one that was marked.
      if (S.values.hook_text && !hookWords(v).includes(hookWords(S.values.hook_text))) S.values.hook_text = "";
      if (S.values.impact_word && !v.toLowerCase().includes(S.values.impact_word.toLowerCase())) S.values.impact_word = "";
      try {
        const folded = v.toLocaleLowerCase();
        const valid = JSON.parse(S.values.hook_keywords || "[]")
          .filter(k => String(k || "").trim() && folded.includes(String(k).trim().toLocaleLowerCase()));
        S.values.hook_keywords = JSON.stringify(valid.slice(0, 14));
      } catch (e) { S.values.hook_keywords = "[]"; }
      completeStep("script", isOthersVsKing() ? "reasoning" : "source");
    }, "primary"));
    c.appendChild(foot);
    // Structure the 2-column card explicitly: LEFT = script editing (box, hook bar, narrator,
    // speaker toggle); RIGHT = the Script Creator generator. Without this the grid auto-placed the
    // loose children into a scattered, "buggy"-looking arrangement.
    (function structureScriptCols() {
      const head = c.querySelector(".proto-config-head");
      const genRow = c.querySelector(".script-gen-row");
      const genInstr = c.querySelector(".generator-instructions");
      const editCol = el("div", "script-edit-col");
      const genCol = el("div", "script-gen-col");
      if (genRow) genCol.appendChild(genRow);
      if (genInstr) genCol.appendChild(genInstr);
      [...c.children].forEach(ch => {
        if (ch === head || ch === foot || ch === editCol || ch === genCol) return;
        editCol.appendChild(ch);          // everything else = the left editing column
      });
      c.insertBefore(editCol, foot);
      c.insertBefore(genCol, foot);
    })();
    setComposer("off");
    return;
  }

  // (narrator now lives in the script step; no separate voice step. hook + impact too.)

  // step: visual source (edit-pipeline chooser removed - every run is v0.2)
  // NOTE: gated on the SCRIPT step now that the separate voice step is gone (was done("voice"),
  // which never becomes true anymore -> the whole flow rendered blank after the first Continue).
  if (done("script")) {
    msgA(esc(T.visual_source_q));
    if (done("source")) {
      msgU(S.values.clip_source === "scrape"
        ? `${esc(T.src_scrape)} · ${esc(T.engine_v2)}`
        : `${esc(T.src_generate)} · ${esc(S.values.video_model)}`, "source");
    } else if (S.step === "source") {
      renderSourceCard(); setComposer("off"); return;
    }
  }

  // step: reasoning model
  if (done("source") || (isOthersVsKing() && done("script"))) {
    msgA(esc(T.reasoning_q));
    if (done("reasoning")) {
      msgU(esc(labelFor(OPT.reasoning_model, S.values.reasoning_model)), "reasoning");
    } else if (S.step === "reasoning") {
      const c = card();
      c.appendChild(choiceButtons(OPT.reasoning_model, S.values.reasoning_model, v => {
        S.values.reasoning_model = v;
        S.values.reasoning_mode = reasoningOptions(v, S.values.reasoning_mode).value;
        renderAll(); persist();
      }));
      const rm = reasoningOptions(S.values.reasoning_model, S.values.reasoning_mode);
      if (rm.options.length) {
        S.values.reasoning_mode = rm.value;
        const rmField = selectField("Reasoning mode", rm.options, rm.value, v => S.values.reasoning_mode = v);
        rmField.style.marginTop = "26px";     // keep "Reasoning mode" clear of the model buttons above
        c.appendChild(rmField);
        c.appendChild(el("div", "hint", "Higher reasoning can improve difficult tasks but may increase response time and cost."));
      }
      const foot = el("div", "card-foot");
      foot.appendChild(btn(T.back, () => editStep(isOthersVsKing() ? "script" : "source"), "ghost"));
      foot.appendChild(btn("Continue", () => completeStep("reasoning", isOthersVsKing() ? "review" : "outputs"), "primary"));
      c.appendChild(foot);
      setComposer("off"); return;
    }
  }

  // step: outputs (the outputs summary chip is intentionally not echoed anymore)
  if (done("reasoning")) {
    msgA(esc(T.outputs_q));
    if (!done("outputs") && S.step === "outputs") {
      renderOutputsCard(); setComposer("off"); return;
    }
  }

  // step: review
  if (done("outputs") || (isOthersVsKing() && done("reasoning"))) {
    msgA(esc(T.review_q));
    if (S.step === "review" && !S.jobId) { renderReviewCard(); setComposer("off"); return; }
  }
  setComposer("off");
}

function renderHookCard() {
  const c = card();
  c.appendChild(el("div", "card-cap", esc(T.mark_hook_q)));
  c.appendChild(el("div", "card-note", esc(T.mark_hook_hint)));
  const view = el("div", "hook-view"); view.id = "hook-view";
  paintHook(view);
  c.appendChild(view);
  // Capture the selection WHILE it exists (inside the script view). Clicking a "Mark" button
  // moves focus and clears window.getSelection(), so we remember the last valid selection.
  let lastSel = "";
  const grab = () => {
    const sel = window.getSelection();
    const txt = sel ? String(sel.toString() || "").trim() : "";
    if (txt && view.contains(sel.anchorNode) && (S.values.script || "").includes(txt)) lastSel = txt;
  };
  view.addEventListener("mouseup", grab);
  view.addEventListener("keyup", grab);
  const st = el("div", "hook-status " + (S.values.hook_text ? "on" : "off"),
    S.values.hook_text ? esc(T.hook_marked) : esc(T.no_hook));
  st.style.marginTop = "8px"; c.appendChild(st);
  // #impact-word: mark ONE word the SFX Master must hit with the big impact/riser in the first 0-5s
  const ist = el("div", "hook-status impact " + (S.values.impact_word ? "on" : "off"),
    S.values.impact_word ? (T.impact_marked + ": " + esc(S.values.impact_word)) : esc(T.no_impact));
  ist.style.marginTop = "6px"; c.appendChild(ist);
  const paintImpactStatus = () => {
    ist.className = "hook-status impact " + (S.values.impact_word ? "on" : "off");
    ist.innerHTML = S.values.impact_word ? (esc(T.impact_marked) + ": <b>" + esc(S.values.impact_word) + "</b>") : esc(T.no_impact);
  };
  const foot = el("div", "card-foot");
  foot.appendChild(btn(T.back, () => editStep("voice"), "ghost"));
  foot.appendChild(btn("★ " + T.mark_hook, () => {
    grab();
    if (lastSel && (S.values.script || "").includes(lastSel)) S.values.hook_text = lastSel;
    paintHook(view); st.className = "hook-status " + (S.values.hook_text ? "on" : "off");
    st.textContent = S.values.hook_text ? T.hook_marked : T.no_hook;
  }));
  foot.appendChild(btn(T.use_first_line, () => {
    const first = (S.values.script || "").split(/(?<=[.!?])\s+|\n/)[0] || "";
    S.values.hook_text = first.trim();
    paintHook(view); st.className = "hook-status on"; st.textContent = T.hook_marked;
  }, "ghost"));
  foot.appendChild(btn("⚡ " + T.mark_impact, () => {
    grab();
    // the impact word is ONE word - take the first token of the selection
    const w = (lastSel || "").split(/\s+/)[0].replace(/[^\p{L}\p{N}'-]/gu, "");
    if (w && (S.values.script || "").toLowerCase().includes(w.toLowerCase())) { S.values.impact_word = w; paintHook(view); paintImpactStatus(); }
  }, "ghost"));
  foot.appendChild(btn(T.clear_hook, () => {
    S.values.hook_text = ""; S.values.impact_word = ""; paintHook(view);
    st.className = "hook-status off"; st.textContent = T.no_hook; paintImpactStatus();
  }, "ghost"));
  foot.appendChild(el("span", "spacer"));
  foot.appendChild(btn(T.continue, () => completeStep("hook", "source"), "primary"));
  c.appendChild(foot);
}
function paintHook(view) {
  const sc = S.values.script || ""; const hk = S.values.hook_text || "";
  const iw = (S.values.impact_word || "").trim();
  let html;
  if (hk && sc.includes(hk)) {
    const i = sc.indexOf(hk);
    html = esc(sc.slice(0, i)) + "<mark>" + esc(hk) + "</mark>" + esc(sc.slice(i + hk.length));
  } else html = esc(sc);
  if (iw) {
    // wrap the first standalone occurrence of the impact word (⚡ = the SFX Master's impact anchor)
    try {
      const re = new RegExp("([^\\p{L}\\p{N}>]|^)(" + iw.replace(/[.*+?^${}()|[\]\\]/g, "\\$&") +
        ")(?![\\p{L}\\p{N}])", "iu");
      html = html.replace(re, (m, pre, w) => pre + '<mark class="impact">' + w + "</mark>");
    } catch (e) {}
  }
  view.innerHTML = html;
}

function renderSourceCard() {
  const c = card();
  if (isCultureFacts()) applyCultureFactsPreset();
  if (isCultureFacts()) {
    c.appendChild(el("div", "card-note", "Clip Short uses V4: one editorial plan, native vertical footage, and no legacy scraper fallback."));
  } else {
    S.values.clip_source = "generate";
    c.appendChild(el("div", "card-note", "Create a short uses generated visual media. Choose the video and image models below."));
  }
  const wrap = el("div", "proto-source-settings"); c.appendChild(wrap);

  if (S.values.clip_source === "generate") {
    wrap.appendChild(selectField(T.video_model, OPT.video_model, S.values.video_model,
      v => S.values.video_model = v));
    wrap.appendChild(selectField(T.image_model, OPT.image_model, S.values.image_model,
      v => S.values.image_model = v));
  } else {
    // V4 is the only Clip Short scraper. Keeping a legacy selector here made
    // saved V2/V3 choices silently bypass the V4 quality gates.
    S.values.scraping_engine = "v4";
    if (!isDiscovery()) {
      wrap.appendChild(el("div", "card-cap", "Clip engine"));
      wrap.appendChild(el("div", "out-sec-hint",
        "V4 · rendered TikTok discovery, then visual review. It rejects boxed captions, letterboxing, stalls and repeated sources before it assigns the timeline."));
      if (S.values.v4_tiktok_discovery_provider === undefined) S.values.v4_tiktok_discovery_provider = "scrapedo";
      if (!S.values.v4_scrapedo_geo) S.values.v4_scrapedo_geo = "jp";
      wrap.appendChild(selectField("TikTok discovery", [
        { value: "scrapedo", label: "Scrape.do rendered search" },
        { value: "brightdata", label: "Bright Data dataset search" }
      ], S.values.v4_tiktok_discovery_provider, (v, initial) => {
        S.values.v4_tiktok_discovery_provider = v;
        if (!initial) { persist(); renderAll(); }
      }));
      if (S.values.v4_tiktok_discovery_provider === "scrapedo") {
        wrap.appendChild(selectField("Search region", [
          { value: "jp", label: "Japan" },
          { value: "sg", label: "Singapore / wider Asia" },
          { value: "us", label: "United States / English" }
        ], S.values.v4_scrapedo_geo, (v, initial) => {
          S.values.v4_scrapedo_geo = v;
          if (!initial) persist();
        }));
      } else {
        const zone = el("div", "fld");
        zone.appendChild(el("label", "", "Bright Web Unlocker zone"));
        const zoneInput = el("input");
        zoneInput.type = "text"; zoneInput.autocomplete = "off";
        zoneInput.placeholder = "e.g. shortslab_v4_unlocker";
        zoneInput.value = S.values.bright_unlocker_zone || "";
        zoneInput.addEventListener("input", () => { S.values.bright_unlocker_zone = zoneInput.value.trim(); persist(); });
        zone.appendChild(zoneInput);
        wrap.appendChild(zone);
      }
    }
    if (isDiscovery()) {
      S.values.scraping_engine = "v4";
      // Discovery recuts one coherent long source. A detached influencer opener would break
      // that source story, so the Clip Short hook option is intentionally not applied here.
      wrap.appendChild(el("div", "out-sec-hint",
        "Discovery searches TikTok for one coherent native 9:16 source, rejects black bars and frame stalls, and opens on its strongest on-topic moment."));
    } else {
      // Relevance controls belong to multi-source Scrape V2. Discovery ranks whole videos.
      wrap.appendChild(el("div", "card-cap", esc(T.script_relevancy)));
      const rr = el("div", "range-row");
      const rg = el("input"); rg.type = "range"; rg.min = 0; rg.max = 100; rg.step = 5;
      rg.value = S.values.script_relevancy || "90";
      const rv = el("span", "range-val", (S.values.script_relevancy || "90") + "%");
      rg.addEventListener("input", () => { S.values.script_relevancy = rg.value; rv.textContent = rg.value + "%"; persist(); });
      rr.appendChild(rg); rr.appendChild(rv); wrap.appendChild(rr);
    }
    // Sort dropdown removed - the scrape now ALWAYS runs every sort order (liked/relevance/
    // viewed/recent) and merges the unique clips, so there is nothing to choose.
    S.values.scrape_sort = "ALL";
    // custom terms chips
    wrap.appendChild(el("div", "card-cap", esc(T.custom_terms)));
    const chips = el("div", "chips"); chips.style.marginBottom = "8px";
    const redraw = () => {
      chips.innerHTML = "";
      terms().forEach((t, i) => {
        const ch = el("span", "chip", esc(t));
        const x = el("button", "", "✕"); x.setAttribute("aria-label", "Remove term");
        x.addEventListener("click", () => { const a = terms(); a.splice(i, 1); S.values.scrape_terms = a.join(", "); redraw(); persist(); });
        ch.appendChild(x); chips.appendChild(ch);
      });
    };
    const terms = () => (S.values.scrape_terms || "").split(",").map(s => s.trim()).filter(Boolean);
    redraw();
    const add = el("div", "chip-add");
    const inp = el("input"); inp.type = "text"; inp.placeholder = T.term_placeholder;
    const plus = btn("+", () => {
      const v = inp.value.trim(); if (!v) return;
      S.values.scrape_terms = terms().concat([v]).join(", "); inp.value = ""; redraw(); persist();
    });
    inp.addEventListener("keydown", e => { if (e.key === "Enter") { e.preventDefault(); plus.click(); } });
    add.appendChild(inp); add.appendChild(plus);
    wrap.appendChild(chips); wrap.appendChild(add);
    // V4 discovers TikTok posts remotely. The local login may only open an already discovered
    // post to deliver its media; it is never a second keyword-search scraper.
    S.values.scrape_platforms = "tiktok,instagram";
    if (S.values.v4_instagram_enabled === undefined) S.values.v4_instagram_enabled = true;
    if (S.values.v4_tiktok_login_fallback === undefined) S.values.v4_tiktok_login_fallback = true;
    wrap.appendChild(el("div", "card-cap", "Data source"));
    wrap.appendChild(el("div", "out-sec-hint",
      "Rendered TikTok results + optional Bright Instagram Reels. Selected TikTok posts use your existing login only for media delivery."));
    if (!isDiscovery()) {
      wrap.appendChild(toggleField("Use TikTok login only when post delivery fails", !!S.values.v4_tiktok_login_fallback,
        v => S.values.v4_tiktok_login_fallback = v));
      wrap.appendChild(toggleField("Include Bright Instagram Reels", !!S.values.v4_instagram_enabled,
        v => S.values.v4_instagram_enabled = v));
      const accounts = el("div", "fld");
      accounts.appendChild(el("label", "", "Instagram reel accounts (optional)"));
      const accountsInput = el("input"); accountsInput.type = "text"; accountsInput.autocomplete = "off";
      accountsInput.placeholder = "@creator_one, @creator_two";
      accountsInput.value = S.values.v4_instagram_accounts || "";
      accountsInput.addEventListener("input", () => { S.values.v4_instagram_accounts = accountsInput.value; persist(); });
      accounts.appendChild(accountsInput);
      accounts.appendChild(el("div", "out-sec-hint",
        "Leave blank and V4 plans relevant raw-footage creators. Bright fetches their recent Reels by profile URL."));
      wrap.appendChild(accounts);
    }
    // SEARCH TIME. How long a topic is worth searching for is an editorial call: a subject with
    // plenty of footage is done in twenty minutes, a scarce one needs the recovery rounds that
    // a short deadline cuts off. Minutes in the UI, seconds in the payload.
    if (!S.values.scrape_time_budget) S.values.scrape_time_budget = "3600";
    const tb = el("div", "fld");
    const tbLbl = el("label", "", "Search time");
    tb.appendChild(tbLbl);
    const tbRow = el("div", "voice-inline");
    const tbIn = el("input");
    tbIn.type = "range"; tbIn.min = "1800"; tbIn.max = "7200"; tbIn.step = "300";
    tbIn.value = String(S.values.scrape_time_budget);
    const tbOut = el("span", "chip");
    const showTb = () => { tbOut.textContent = Math.round(+tbIn.value / 60) + " min"; };
    showTb();
    tbIn.addEventListener("input", () => {
      S.values.scrape_time_budget = String(tbIn.value); showTb(); persist();
    });
    tbRow.appendChild(tbIn); tbRow.appendChild(tbOut);
    tb.appendChild(tbRow);
    tb.appendChild(el("div", "card-note",
      "Upper limit, not a target - the search stops as soon as every beat has a real choice of footage."));
    wrap.appendChild(tb);
    // (Background music picker removed by request - runs never add background music.)
  }

  const foot = el("div", "card-foot");
  foot.appendChild(btn(T.back, () => editStep("voice"), "ghost"));
  foot.appendChild(el("span", "spacer"));
  foot.appendChild(btn(T.continue, () => {
    // scrape disables the AI-image outputs exactly like the legacy form did
    if (S.values.clip_source === "scrape") {
      S._prevOut = { web: S.values.out_web_images, wiki: S.values.out_wikimedia, gpt: S.values.out_gpt_images };
      S.values.out_web_images = false; S.values.out_wikimedia = false; S.values.out_gpt_images = false;
      S.values.out_video_clips = true;
    } else if (S._prevOut) {
      S.values.out_web_images = S._prevOut.web; S.values.out_wikimedia = S._prevOut.wiki;
      S.values.out_gpt_images = S._prevOut.gpt; S._prevOut = null;
    }
    completeStep("source", "reasoning");
  }, "primary"));
  c.appendChild(foot);
}

function renderVoiceCard() {
  const c = card();
  if (isCultureFacts()) applyCultureFactsPreset();
  // narrator field: dropdown + Preview button side-by-side (preview sits to the RIGHT of the voice select)
  const voiceFld = el("div", "fld voice-fld");
  voiceFld.appendChild(el("label", "", esc(T.tts_voice)));
  const voiceInline = el("div", "voice-inline");
  const vsel = el("select");
  resetTtsVoice(S.values);
  ttsVoiceOptions(S.values.tts_model).forEach(o => vsel.appendChild(new Option(o.label, o.value)));
  if (S.values.tts_voice && [...vsel.options].some(o => o.value === S.values.tts_voice)) vsel.value = S.values.tts_voice;
  S.values.tts_voice = vsel.value;
  vsel.addEventListener("change", () => { S.values.tts_voice = vsel.value; persist(); });
  const prev = btn("▶ " + T.preview, () => {
    previewTts(S.values);
  }, "ghost small");
  voiceInline.appendChild(vsel); voiceInline.appendChild(prev);
  voiceFld.appendChild(voiceInline);
  const row = el("div", "fld-row");
  row.appendChild(voiceFld);
  row.appendChild(selectField(T.tts_model, OPT.tts_model, S.values.tts_model, v => {
    S.values.tts_model = v; resetTtsVoice(S.values); persist(); renderAll();
  }));
  c.appendChild(row);
  const seedSettings = seedTtsSettings(S.values); if (seedSettings) c.appendChild(seedSettings);
  // "fresh voice take" slider hidden by request (force_regenerate still defaults false in state)
  if (!isCultureFacts()) c.appendChild(toggleField(T.speaker_video, S.values.enable_speaker_hook, v => {
    S.values.enable_speaker_hook = v; renderAll(); persist();
  }));
  if (!isCultureFacts() && S.values.enable_speaker_hook) {
    c.appendChild(el("div", "card-cap", esc(T.speaker_image)));
    const grid = el("div", "spk-grid"); grid.id = "spk-grid";
    c.appendChild(grid);
    loadSpeakerGallery(grid);
    const up = btn("⬆ " + T.upload_image, () => pickFile("image/*", f => {
      FILES.speaker_image_file = f;
      S.values.speaker_image_path = "";
      const note = el("div", "upl-file");
      note.innerHTML = `<span class="nm">${esc(f.name)}</span>`;
      grid.parentElement.insertBefore(note, grid.nextSibling);
    }), "ghost small");
    up.style.marginTop = "8px"; c.appendChild(up);
  }
  const foot = el("div", "card-foot");
  foot.appendChild(btn(T.back, () => editStep("script"), "ghost"));
  foot.appendChild(el("span", "spacer"));
  foot.appendChild(btn(T.continue, () => completeStep("voice", "source"), "primary"));
  c.appendChild(foot);
}
function loadSpeakerGallery(grid) {
  // speaker images come from the legacy form markup (single source of truth)
  fetch("/?legacy_ui=1").then(r => r.text()).then(html => {
    const m = html.match(/class="speaker-grid"[\s\S]*?<\/div>\s*<\/div>/);
    const urls = [];
    const re = /data-path="([^"]+)"[^>]*>\s*<img[^>]*src="([^"]+)"/g;
    let mm; while ((mm = re.exec(html))) urls.push({ path: mm[1], src: mm[2] });
    if (!urls.length) { grid.innerHTML = `<span class="card-note">${esc(T.upload_image)}</span>`; return; }
    urls.slice(0, 40).forEach(u => {
      const b = el("button", "spk" + (S.values.speaker_image_path === u.path ? " sel" : ""));
      b.innerHTML = `<img loading="lazy" src="${esc(u.src)}" alt="">`;
      b.addEventListener("click", () => {
        S.values.speaker_image_path = u.path; FILES.speaker_image_file = null;
        grid.querySelectorAll(".spk").forEach(x => x.classList.remove("sel"));
        b.classList.add("sel"); persist();
      });
      grid.appendChild(b);
    });
  }).catch(() => {});
}

const OUTPUT_FIELDS = [
  ["out_web_images", "Web images"], ["out_wikimedia", "Wikimedia images"],
  ["out_gpt_images", "Generated images"], ["out_video_clips", "Video clips"],
  ["out_sfx", "Sound effects"], ["out_transition_sfx", "Transition SFX"],
  ["out_captions", "Captions"],
];
// low/medium/high amount as a 3-stop SLIDER (reused for SFX + VFX amount, in the flow and masters)
function amountSliderField(label, current, onChange) {
  const levels = ["low", "medium", "high"];
  const wrap = el("div", "fld");
  wrap.appendChild(el("label", "", esc(label)));
  const row = el("div", "range-row");
  const rg = el("input"); rg.type = "range"; rg.min = 0; rg.max = 2; rg.step = 1;
  rg.value = Math.max(0, levels.indexOf(current || "medium"));
  const val = el("span", "range-val", levels[+rg.value]);
  rg.addEventListener("input", () => { val.textContent = levels[+rg.value]; onChange(levels[+rg.value]); persist(); });
  row.appendChild(rg); row.appendChild(val); wrap.appendChild(row);
  return wrap;
}
function renderOutputsCard() {
  const c = card("layers-card");
  if (isCultureFacts()) applyCultureFactsPreset();
  const scrape = S.values.clip_source === "scrape";
  // one clean section = a compact header + a body of controls, evenly spaced.
  const section = (title, ...nodes) => {
    outSection(c, title, ...nodes);
  };
  const grid = (fields) => {
    const g = el("div", "tgl-grid");
    fields.forEach(([k, label]) => g.appendChild(toggleField(label, S.values[k], v => S.values[k] = v)));
    return g;
  };
  // Media layer — only for AI Short (generate). Clip Short always uses the scraped footage.
  if (!scrape) {
    section("Media", grid([
      ["out_video_clips", "Video clips"], ["out_gpt_images", "AI images"],
      ["out_web_images", "Web images"], ["out_wikimedia", "Wikimedia"],
    ]));
  }
  // Visual effects (arrows/callouts baked in-render)
  const vg = el("div", "tgl-grid");
  vg.appendChild(toggleField("Arrows & callouts", S.values.add_visual_effects !== false, v => S.values.add_visual_effects = v));
  section("Visual effects", vg, amountSliderField("Amount", S.values.vfx_amount || "medium", v => S.values.vfx_amount = v));
  // Sound. Real-footage Clip Shorts expose the actual backend policy instead of a broad
  // "Sound effects" switch that misleadingly suggested semantic impacts/foley were enabled.
  if (scrape) {
    section("Sound", grid([["out_transition_sfx", "Hook riser & cut SFX"]]),
      amountSliderField("Amount", S.values.sfx_amount, v => S.values.sfx_amount = v),
      el("div", "out-sec-hint", "Only quiet whoosh, swish, pop and click accents. No ambience or content SFX."));
  } else {
    section("Sound", grid([["out_sfx", "Sound effects"], ["out_transition_sfx", "Transition SFX"]]),
      amountSliderField("Amount", S.values.sfx_amount, v => S.values.sfx_amount = v));
  }
  // Captions — word-by-word toggle + the fully user-customizable style (live preview)
  section("Captions", grid([["out_captions", "Word-by-word captions"]]), captionStylePanel());
  // Speech approval (not in Discovery - its approval moment is the material pick)
  if (!isDiscovery()) {
    const halt = toggleField(T.halt_after_speech, S.values.halt_after_speech, v => S.values.halt_after_speech = v);
    section("Speech", halt, el("div", "out-sec-hint", "Pause to approve or re-do the voice before the run finishes."));
  }
  const foot = el("div", "card-foot");
  foot.appendChild(btn(T.back, () => editStep("reasoning"), "ghost"));
  foot.appendChild(el("span", "spacer"));
  foot.appendChild(btn(T.continue, () => completeStep("outputs", "review"), "primary"));
  c.appendChild(foot);
}

async function openCandidateLibrary(host, onPick) {
  document.querySelectorAll(".cand-lib-card").forEach(x => x.remove());
  let d = null;
  try { d = await jget("/candidate-library"); } catch (e) { d = null; }
  const card = el("div", "chat-card cand-lib-card");
  if (host.parentElement) host.parentElement.insertBefore(card, host.nextSibling);
  else chat.appendChild(card);
  const head = el("div", "");
  head.style.cssText = "display:flex; align-items:center; justify-content:space-between; margin-bottom:8px;";
  head.appendChild(el("h3", "", "Candidate library"));
  head.appendChild(btn("Close", () => card.remove(), "ghost small"));
  card.appendChild(head);
  const rows = (d && d.candidates) || [];
  if (!rows.length) {
    card.appendChild(el("div", "card-note", "No saved candidates yet — every discovery/mini run stores the candidates it shows here."));
    return;
  }
  card.appendChild(el("div", "card-note", rows.length + " candidates from past runs. Pick one to build a short directly from it (no search)."));
  const grid = el("div", "");
  grid.style.cssText = "display:flex; gap:10px; flex-wrap:wrap; margin-top:8px; max-height:520px; overflow-y:auto;";
  card.appendChild(grid);
  rows.forEach(r => {
    const it = el("div", "");
    it.style.cssText = "flex:1 1 210px; max-width:250px; border:1px solid var(--line-strong); border-radius:10px; padding:9px; background:var(--bg-input);";
    it.appendChild(el("strong", "", esc(r.title || r.desc || "Candidate")));
    const meta = `@${esc(r.author || "")} · ${r.dur}s · ${(+r.likes || 0).toLocaleString()} likes`
      + (r.appeal ? ` · appeal ${r.appeal}/10` : "") + (r.picked ? " · ✓ used" : "");
    it.appendChild(el("div", "card-note", meta));
    if (r.premise) it.appendChild(el("div", "card-note", "“" + esc(r.premise) + "”"));
    if (r.video_url) {
      const vp = document.createElement("video");
      vp.src = r.video_url; vp.controls = true; vp.preload = "metadata";
      vp.style.cssText = "width:100%; border-radius:7px; margin:5px 0; max-height:340px; background:#000;";
      it.appendChild(vp);
    } else if (r.sheet_url) {
      const im = document.createElement("img");
      im.src = r.sheet_url; im.loading = "lazy";
      im.style.cssText = "width:100%; border-radius:7px; margin:5px 0;";
      it.appendChild(im);
    }
    it.appendChild(el("div", "card-note", esc(r.last_seen || "")));
    it.appendChild(btn("Use this candidate", () => {
      S.values.candidate_url = r.url || "";
      persist();
      card.remove();
      if (typeof onPick === "function") onPick();
    }, "primary small"));
    grid.appendChild(it);
  });
}

function captionStylePanel() {
  // Fully user-customizable caption style (applies to every clip-short mode; the render
  // reads the same keys, and the timeline editor mirrors them from project.json).
  const V = S.values;
  if (!V.caption_active_style || V.caption_active_style === "box") V.caption_active_style = "none";
  const wrap = el("div", "capstyle");
  // live preview
  const prev = el("div", "capstyle-preview");
  const paintPreview = () => {
    const up = (V.caption_uppercase_choice || "upper") !== "normal";
    const base = V.caption_base_color || "#ffffff";
    const act = V.caption_active_color || "#ffffff";
    const strokeMode = V.caption_stroke || "thin";
    const sz = Math.max(13, Math.round((+V.caption_size || 84) * 0.28));
    const shadow = strokeMode === "none" ? "none"
      : strokeMode === "bold"
        ? "-2px 0 0 #000, 2px 0 0 #000, 0 -2px 0 #000, 0 2px 0 #000, 2px 3px 3px rgba(0,0,0,.6)"
        : "-1px 0 0 #000, 1px 0 0 #000, 0 -1px 0 #000, 0 1px 0 #000, 1px 2px 2px rgba(0,0,0,.5)";
    const w = (t) => `<span style="color:${base}; font-weight:800; font-size:${sz}px; text-shadow:${shadow};">${t}</span>`;
    const at = up ? "WORD" : "word";
    let activeHtml;
    if (V.caption_active_style === "none") {
      activeHtml = w(at);
    } else {
      activeHtml = `<span style="color:${act}; font-weight:800; font-size:${sz}px; text-shadow:${shadow};">${at}</span>`;
    }
    prev.innerHTML = w(up ? "EVERY" : "every") + activeHtml + w(up ? "POPS" : "pops");
  };
  wrap.appendChild(prev);
  // segmented highlight control
  const seg = el("div", "capstyle-seg");
  [["color", "Colored word"], ["none", "Plain"]].forEach(([val, label]) => {
    const b = el("button", "capstyle-seg-btn", esc(label));
    b.type = "button";
    b.dataset.capstyle = val;
    b.addEventListener("click", () => {
      V.caption_active_style = val; persist(); paintSeg(); paintFields(); paintPreview();
    });
    seg.appendChild(b);
  });
  const paintSeg = () => seg.querySelectorAll("button").forEach(x =>
    x.classList.toggle("on", x.dataset.capstyle === V.caption_active_style));
  wrap.appendChild(seg);
  // labeled control grid
  const grid = el("div", "capstyle-grid");
  const field = (label, control) => {
    const f = el("div", "capstyle-field");
    f.appendChild(el("span", "capstyle-lbl", esc(label)));
    f.appendChild(control);
    grid.appendChild(f);
    return f;
  };
  const colorInput = (key, fallback) => {
    const inp = document.createElement("input");
    inp.type = "color"; inp.value = V[key] || fallback;
    inp.className = "capstyle-color";
    inp.addEventListener("input", () => { V[key] = inp.value; persist(); paintPreview(); });
    return inp;
  };
  const fldActive = field("Active word", colorInput("caption_active_color", "#ffffff"));
  field("Text", colorInput("caption_base_color", "#ffffff"));
  // only show the controls the chosen highlight mode actually uses (the box mode is gone)
  const paintFields = () => {
    fldActive.style.display = V.caption_active_style === "color" ? "" : "none";
  };
  const strokeSel = el("select");
  [["thin", "Thin outline"], ["bold", "Bold outline"], ["none", "No outline"]].forEach(([v, l]) => strokeSel.appendChild(new Option(l, v)));
  strokeSel.value = V.caption_stroke || "thin";
  strokeSel.addEventListener("change", () => { V.caption_stroke = strokeSel.value; persist(); paintPreview(); });
  field("Outline", strokeSel);
  const caseSel = el("select");
  [["upper", "UPPERCASE"], ["normal", "Normal case"]].forEach(([v, l]) => caseSel.appendChild(new Option(l, v)));
  caseSel.value = V.caption_uppercase_choice || "upper";
  caseSel.addEventListener("change", () => { V.caption_uppercase_choice = caseSel.value; persist(); paintPreview(); });
  field("Case", caseSel);
  const size = document.createElement("input");
  size.type = "range"; size.min = "54"; size.max = "120"; size.step = "2";
  size.value = String(+V.caption_size || 84);
  size.addEventListener("input", () => { V.caption_size = size.value; persist(); paintPreview(); });
  field("Size", size);
  wrap.appendChild(grid);
  paintSeg(); paintFields(); paintPreview();
  return wrap;
}

function renderAIShortPicker() {
  msgA("Choose how the AI should build the visual world.");
  const c = card("ai-short-picker");
  const grid = el("div", "ai-short-choice-grid");
  const classic = el("button", "ai-short-choice classic");
  classic.type = "button";
  classic.innerHTML = `<span class="ai-choice-preview"><video muted autoplay loop playsinline preload="metadata" src="/file?path=static%2Fpreviews%2Fai_short_pig_war_motion_20s_hd.mp4"></video></span>
    <span class="ai-choice-copy"><small>NARRATED · MULTI-SCENE</small><strong>Visuals from Script</strong>
    <em>The existing AI Short pipeline: a complete narrated edit made from AI video, generated images and web imagery.</em></span>`;
  classic.addEventListener("click", () => selectMode("visualscript-classic"));
  const motion = el("button", "ai-short-choice motion-loop");
  motion.type = "button";
  motion.innerHTML = `<span class="ai-choice-preview"><video muted autoplay loop playsinline preload="metadata" poster="/file?path=static%2Fpreviews%2Fmotion_loop_poster.jpg&v=4" src="/file?path=static%2Fpreviews%2Fmotion_loop_prototype.mp4&v=4"></video><i class="ai-loop-orbit"></i></span>
    <span class="ai-choice-copy"><small>PROMPTS OUT · CLIPS BACK IN</small><strong>AI Core</strong>
    <em>Describe a short, pick a storyline, copy three prompts that share one world — then drop the generated clips back in.</em></span>`;
  motion.addEventListener("click", () => selectMode("aicore"));
  const dream = el("button", "ai-short-choice dreamcore");
  dream.type = "button";
  dream.innerHTML = `<span class="ai-choice-preview dc-preview"><i class="dc-glow"></i></span>
    <span class="ai-choice-copy"><small>PROMPTS WITH CUTS · EDIT ON THE MELODY</small><strong>Dreamcore</strong>
    <em>Empty liminal places, no voice. The prompts already contain their own cuts, and the upload is cut so every cut lands on a note of the music.</em></span>`;
  dream.addEventListener("click", () => selectMode("dreamcore"));
  grid.append(classic, motion, dream); c.appendChild(grid);
  const foot = el("div", "card-foot"); foot.appendChild(btn("All modes", resetToMode, "ghost")); c.appendChild(foot);
  c.querySelectorAll("video").forEach(v => { v.muted = true; v.play().catch(() => {}); });
  setComposer("off");
}

/* AI Core: describe a short, pick a storyline, copy three prompts, drop the clips back in.
   The clips are generated outside this app, so the flow's job is to hand over three prompts
   that hold one world together and then take the results back in the right order. */
const AICORE_FILES = [null, null, null];

function aicoreState() {
  if (!S.aicore) S.aicore = { brief: "", options: [], chosen: null, world: "", prompts: [] };
  return S.aicore;
}

function renderAICoreFlow() {
  const A = aicoreState();
  msgU("AI Core");
  if (S.jobId) return;

  /* ---- step 1: the idea ---- */
  if (S.step === "brief") {
    msgA("Describe the short in a sentence or two. Everything else is built from it.");
    const c = card("aicore-brief");
    const label = el("label", "aicore-label");
    // Dreamcore has its own mode now, so the examples here no longer point at it.
    label.innerHTML = `<span>Your idea</span><small>Name the character and the point of view first — "SpongeBob one day POV", "a courier's helmet cam through Tokyo". Three clips will be written for one world.</small>`;
    const ta = document.createElement("textarea");
    ta.rows = 5;
    ta.id = "aicore-brief-input";
    ta.placeholder = "e.g. SpongeBob one day POV, morning to closing time at the Krusty Krab";
    ta.value = A.brief || "";
    ta.addEventListener("input", () => { A.brief = ta.value; persist(); });
    label.appendChild(ta);
    c.appendChild(label);
    const foot = el("div", "card-foot");
    foot.appendChild(btn("All modes", resetToMode, "ghost"));
    const go = btn("Continue", async () => {
      const brief = (A.brief || "").trim();
      if (!brief) { errorCard(T.err_generic, "Describe the short first."); return; }
      go.disabled = true; go.textContent = "Writing storylines…";
      const wait = card("aicore-wait");
      wait.appendChild(el("div", "dots", "<i></i><i></i><i></i>"));
      try {
        const d = await jpost("/aicore-storylines", { brief });
        if (d.error) throw new Error(d.error);
        A.options = d.options || []; A.chosen = null;
        A.world = ""; A.prompts = [];
        AICORE_FILES.fill(null);
        completeStep("brief", "pick");
      } catch (e) {
        wait.remove();
        go.disabled = false; go.textContent = "Continue";
        errorCard(T.err_generic, String(e));
      }
    }, "primary");
    foot.appendChild(go);
    c.appendChild(foot);
    setComposer("off");
    return;
  }

  if (A.brief) msgU(esc(A.brief), "brief");

  /* ---- step 2: pick one of five ---- */
  if (S.step === "pick") {
    msgA("Five ways this could go. Pick the one you want.");
    const c = card("aicore-pick");
    (A.options || []).forEach((o, i) => {
      const b = el("button", "aicore-option");
      b.type = "button";
      const beats = (o.beats || []).filter(Boolean)
        .map(x => `<li>${esc(x)}</li>`).join("");
      b.innerHTML = `<span class="ao-num">${i + 1}</span>
        <span class="ao-body"><strong>${esc(o.title || "")}</strong>
        <em>${esc(o.summary || "")}</em><ol class="ao-beats">${beats}</ol></span>`;
      b.addEventListener("click", async () => {
        c.querySelectorAll(".aicore-option").forEach(x => { x.disabled = true; });
        b.classList.add("on");
        A.chosen = o;
        const wait = card("aicore-wait");
        wait.appendChild(el("div", "dots", "<i></i><i></i><i></i>"));
        try {
          const d = await jpost("/aicore-prompts", { brief: A.brief, storyline: o });
          if (d.error) throw new Error(d.error);
          A.world = d.world || "";
          A.prompts = d.prompts || [];
          AICORE_FILES.fill(null);
          completeStep("pick", "clips");
        } catch (e) {
          wait.remove();
          c.querySelectorAll(".aicore-option").forEach(x => { x.disabled = false; });
          b.classList.remove("on");
          errorCard(T.err_generic, String(e));
        }
      });
      c.appendChild(b);
    });
    const foot = el("div", "card-foot");
    foot.appendChild(btn(T.back, () => editStep("brief"), "ghost"));
    c.appendChild(foot);
    setComposer("off");
    return;
  }

  /* ---- step 3: copy the prompts, drop the clips back in ---- */
  if (S.step === "clips") {
    if (A.chosen) msgU(esc(A.chosen.title || ""), "pick");
    msgA("Generate each clip with these prompts, then drop the files onto their slot.");
    const c = card("aicore-prompts");
    if (A.world) {
      const w = el("div", "aicore-world");
      w.innerHTML = `<span class="aw-k">Shared world · already at the start of every prompt</span>`;
      const p = el("p", "aw-v", esc(A.world));
      w.appendChild(p);
      c.appendChild(w);
    }
    const assembleRow = el("div", "card-foot aicore-foot");
    const go = btn("Assemble video", () => submitAICore(go), "primary");
    const refresh = () => {
      const ready = AICORE_FILES.filter(Boolean).length;
      go.disabled = ready < 1;
      go.textContent = ready >= (A.prompts || []).length
        ? "Assemble video"
        : `Assemble video (${ready}/${(A.prompts || []).length} clips)`;
    };

    (A.prompts || []).forEach((p, i) => {
      const row = el("div", "aicore-prompt");
      const head = el("div", "ap-head");
      head.innerHTML = `<span class="ap-num">Clip ${i + 1}</span><strong>${esc(p.label || "")}</strong>`;
      const copy = el("button", "btn small ap-copy", "Copy prompt");
      copy.type = "button";
      copy.addEventListener("click", async () => {
        try {
          await navigator.clipboard.writeText(p.text || "");
        } catch (e) {
          // clipboard is blocked outside a secure context; select the text so
          // Ctrl+C still works rather than leaving the button silently dead
          const box = row.querySelector(".ap-text");
          if (box) {
            const r = document.createRange(); r.selectNodeContents(box);
            const sel = window.getSelection(); sel.removeAllRanges(); sel.addRange(r);
          }
        }
        copy.textContent = "Copied ✓";
        copy.classList.add("ok");
        setTimeout(() => { copy.textContent = "Copy prompt"; copy.classList.remove("ok"); }, 1600);
      });
      head.appendChild(copy);
      row.appendChild(head);
      const text = el("pre", "ap-text", esc(p.text || ""));
      row.appendChild(text);

      const drop = el("div", "aicore-drop");
      const setName = (f) => {
        drop.classList.toggle("filled", !!f);
        const size = f && (f.size >= 1048576
          ? (f.size / 1048576).toFixed(1) + " MB"
          : Math.max(1, Math.round(f.size / 1024)) + " KB");
        drop.innerHTML = f
          ? `<b>${esc(f.name)}</b><small>${size} · click to replace</small>`
          : `<b>Drop clip ${i + 1} here</b><small>or click to choose a file</small>`;
      };
      setName(null);
      const take = (f) => {
        if (!f) return;
        AICORE_FILES[i] = f; setName(f); refresh();
      };
      drop.addEventListener("click", () => pickFile("video/*,.mp4,.mov,.webm", take));
      drop.addEventListener("dragover", e => { e.preventDefault(); drop.classList.add("over"); });
      drop.addEventListener("dragleave", () => drop.classList.remove("over"));
      drop.addEventListener("drop", e => {
        e.preventDefault(); drop.classList.remove("over");
        take((e.dataTransfer.files || [])[0]);
      });
      row.appendChild(drop);
      c.appendChild(row);
    });

    assembleRow.appendChild(btn(T.back, () => editStep("pick"), "ghost"));
    assembleRow.appendChild(go);
    c.appendChild(assembleRow);
    refresh();
    setComposer("off");
  }
}

async function submitAICore(go) {
  const A = aicoreState();
  const files = AICORE_FILES.filter(Boolean);
  if (!files.length) { errorCard(T.err_generic, "Add at least one clip."); return; }
  go.disabled = true; go.textContent = "Uploading…";
  const fd = new FormData();
  // Numbered names, because the clips must be joined in beat order and the
  // server sorts by field name to get it.
  AICORE_FILES.forEach((f, i) => { if (f) fd.append(`clip${i + 1}`, f, f.name); });
  fd.append("title", (A.chosen && A.chosen.title) || A.brief || "AI Core short");
  fd.append("brief", A.brief || "");
  fd.append("world", A.world || "");
  fd.append("storyline", (A.chosen && A.chosen.summary) || "");
  fd.append("prompts", JSON.stringify(A.prompts || []));
  try {
    const r = await fetch("/aicore-assemble", { method: "POST", body: fd });
    const d = await r.json();
    if (d.error) throw new Error(d.error);
    startJob(d.job_id, "aicore");
  } catch (e) {
    go.disabled = false; go.textContent = "Assemble video";
    errorCard(T.err_generic, String(e));
  }
}

/* Dreamcore: prompts that already contain their cuts, then an edit locked to the melody.

   Two steps only. There is no storyline to pick because there is no story: a dreamcore
   short is a series of empty places, and what makes it work is the cut rhythm, not the
   narrative. The prompts therefore carry hold times taken from the chosen music, and the
   upload step hands the clips straight to the editor that snaps them to it. */
let DREAMCORE_FILES = [];

function dreamcoreState() {
  if (!S.dreamcore) {
    S.dreamcore = { brief: "", world: "", prompts: [], bed: "", grid: null,
                    clips: 2, editStyle: "auto", slug: "" };
  }
  return S.dreamcore;
}

function renderDreamcoreFlow() {
  const D = dreamcoreState();
  msgU("Dreamcore");
  if (S.jobId) return;

  /* ---- step 1: optional direction, music and reference-derived edit language ---- */
  if (S.step === "brief") {
    msgA("Add a direction if you have one, or leave it blank and let the director invent a new dreamcore world.");
    const c = card("dreamcore-brief");
    const label = el("label", "aicore-label");
    label.innerHTML = `<span>Creative direction <em class="optional-mark">Optional</em></span><small>A setting, object, memory, reference note or visual instruction. Blank is fully supported: the agent creates an original concept from the reference style.</small>`;
    const ta = document.createElement("textarea");
    ta.rows = 4;
    ta.id = "dreamcore-brief-input";
    ta.placeholder = "Leave blank for a fully original concept, or add a loose direction…";
    ta.value = D.brief || "";
    ta.addEventListener("input", () => { D.brief = ta.value; persist(); });
    label.appendChild(ta);
    c.appendChild(label);

    const row = el("div", "dc-row");
    const bedWrap = el("label", "aicore-label dc-bed");
    bedWrap.innerHTML = `<span>Music</span><small>The cut grid is read from this track.</small>`;
    const bedSel = document.createElement("select");
    bedSel.id = "dreamcore-bed";
    bedWrap.appendChild(bedSel);
    fetch("/music-list").then(r => r.json()).then(d => {
      (d.tracks || []).forEach(t => {
        const o = document.createElement("option");
        o.value = t.file; o.textContent = t.name;
        if ((D.bed && D.bed === t.file) || (!D.bed && /dreamcore/i.test(t.file))) {
          o.selected = true; D.bed = t.file;
        }
        bedSel.appendChild(o);
      });
    }).catch(() => {});
    bedSel.addEventListener("change", () => { D.bed = bedSel.value; persist(); });

    const num = (key, text, hint, min, max) => {
      const w = el("label", "aicore-label dc-num");
      w.innerHTML = `<span>${text}</span><small>${hint}</small>`;
      const inp = document.createElement("input");
      inp.type = "number"; inp.min = String(min); inp.max = String(max);
      inp.value = String(D[key]);
      inp.addEventListener("input", () => {
        D[key] = Math.max(min, Math.min(max, parseInt(inp.value, 10) || min));
        persist();
      });
      w.appendChild(inp);
      return w;
    };
    const styleWrap = el("label", "aicore-label dc-style");
    styleWrap.innerHTML = `<span>Cut language</span><small>Every Higgsfield generation is exactly 10 seconds.</small>`;
    const styleSel = document.createElement("select");
    [
      ["auto", "Auto mix — varied reference patterns"],
      ["classic_reveal", "Classic reveal — cuts at 3.7s + 7.4s"],
      ["continuous_passage", "Continuous passage — no cuts"],
      ["late_reveal", "Late reveal — one cut at 7.4s"],
      ["memory_glitch", "Memory glitch — rapid cut bursts"],
    ].forEach(([value, text]) => {
      const o = document.createElement("option"); o.value = value; o.textContent = text;
      o.selected = (D.editStyle || "auto") === value; styleSel.appendChild(o);
    });
    styleSel.addEventListener("change", () => { D.editStyle = styleSel.value; persist(); });
    styleWrap.appendChild(styleSel);
    row.append(bedWrap, num("clips", "10s chapters", "How many prompts to create", 1, 12), styleWrap);
    c.appendChild(row);

    const foot = el("div", "card-foot");
    foot.appendChild(btn("All modes", resetToMode, "ghost"));
    const go = btn("Write the prompts", async () => {
      const brief = (D.brief || "").trim();
      go.disabled = true; go.textContent = "Writing…";
      const wait = card("dreamcore-wait");
      wait.appendChild(el("div", "dots", "<i></i><i></i><i></i>"));
      try {
        const d = await jpost("/dreamcore-prompts", {
          brief, bed: D.bed || "", clips: D.clips,
          edit_style: D.editStyle || "auto", clip_seconds: 10 });
        if (d.error) throw new Error(d.error);
        D.prompts = d.prompts || []; D.world = d.world || "";
        D.grid = d.grid || null;
        // The server wrote these prompts into a real project. Holding its slug means the
        // clips finish THAT project later instead of starting a second one, and it is what
        // the sidebar entry reopens.
        D.slug = d.slug || "";
        if (d.slug) { S.projectSlug = d.slug; S.projectTitle = (D.brief || "Dreamcore").slice(0, 60); }
        DREAMCORE_FILES = new Array(D.prompts.length).fill(null);
        wait.remove();
        editStep("clips");
      } catch (e) {
        wait.remove();
        go.disabled = false; go.textContent = "Write the prompts";
        errorCard(T.err_generic, String(e));
      }
    }, "primary");
    foot.appendChild(go);
    c.appendChild(foot);
    setComposer("off");
    return;
  }

  /* ---- step 2: copy the prompts, drop the generated clips back in ---- */
  if (S.step === "clips") {
    const phrase = D.grid && D.grid.phrase ? Number(D.grid.phrase).toFixed(2) : "3.85";
    msgA(`Generate each clip, then drop it on its slot. Every cut will be moved onto the music — a phrase is ${phrase}s.`);
    const c = card("dreamcore-prompts");
    if (D.world) {
      const w = el("div", "aicore-world");
      w.innerHTML = `<span class="aw-k">Collection logic</span>`;
      w.appendChild(el("p", "aw-v", esc(D.world)));
      c.appendChild(w);
    }
    if (DREAMCORE_FILES.length !== (D.prompts || []).length) {
      DREAMCORE_FILES = new Array((D.prompts || []).length).fill(null);
    }
    const footRow = el("div", "card-foot aicore-foot");
    const go = btn("Cut to the music", () => submitDreamcore(go), "primary");
    const refresh = () => {
      const ready = DREAMCORE_FILES.filter(Boolean).length;
      go.disabled = ready < 1;
      go.textContent = ready >= (D.prompts || []).length
        ? "Cut to the music"
        : `Cut to the music (${ready}/${(D.prompts || []).length} clips)`;
    };

    (D.prompts || []).forEach((p, i) => {
      const row = el("div", "aicore-prompt");
      const head = el("div", "ap-head");
      head.innerHTML = `<span class="ap-num">Clip ${i + 1}</span><strong>${esc(p.label || "")}</strong>`;
      const copy = el("button", "btn small ap-copy", "Copy prompt");
      copy.type = "button";
      copy.addEventListener("click", async () => {
        try { await navigator.clipboard.writeText(p.text || ""); }
        catch (e) {
          const box = row.querySelector(".ap-text");
          if (box) {
            const r = document.createRange(); r.selectNodeContents(box);
            const sel = window.getSelection(); sel.removeAllRanges(); sel.addRange(r);
          }
        }
        copy.textContent = "Copied ✓"; copy.classList.add("ok");
        setTimeout(() => { copy.textContent = "Copy prompt"; copy.classList.remove("ok"); }, 1600);
      });
      head.appendChild(copy);
      row.appendChild(head);
      if ((p.shots || []).length) {
        const shots = el("div", "dc-shots");
        shots.innerHTML = (p.shots || [])
          .map((s, n) => `<span class="dc-shot"><i>${n + 1}</i>${esc(s)}</span>`).join("");
        row.appendChild(shots);
      }
      const grammar = el("div", "dc-grammar");
      const cuts = (p.cut_times || []).length
        ? (p.cut_times || []).map(v => Number(v).toFixed(1) + "s").join(" · ")
        : "no internal cuts";
      grammar.textContent = `${String(p.edit_style || "10s chapter").replaceAll("_", " ")} · ${cuts}`;
      row.appendChild(grammar);
      row.appendChild(el("pre", "ap-text", esc(p.text || "")));

      const drop = el("div", "aicore-drop");
      const setName = (f) => {
        drop.classList.toggle("filled", !!f);
        const size = f && (f.size >= 1048576
          ? (f.size / 1048576).toFixed(1) + " MB"
          : Math.max(1, Math.round(f.size / 1024)) + " KB");
        drop.innerHTML = f
          ? `<b>${esc(f.name)}</b><small>${size} · click to replace</small>`
          : `<b>Drop clip ${i + 1} here</b><small>or click to choose a file</small>`;
      };
      setName(null);
      const take = (f) => { if (f) { DREAMCORE_FILES[i] = f; setName(f); refresh(); } };
      drop.addEventListener("click", () => pickFile("video/*,.mp4,.mov,.webm", take));
      drop.addEventListener("dragover", e => { e.preventDefault(); drop.classList.add("over"); });
      drop.addEventListener("dragleave", () => drop.classList.remove("over"));
      drop.addEventListener("drop", e => {
        e.preventDefault(); drop.classList.remove("over");
        take((e.dataTransfer.files || [])[0]);
      });
      row.appendChild(drop);
      c.appendChild(row);
    });

    footRow.appendChild(btn(T.back, () => editStep("brief"), "ghost"));
    footRow.appendChild(go);
    c.appendChild(footRow);
    refresh();
    setComposer("off");
  }
}

async function submitDreamcore(go) {
  const D = dreamcoreState();
  const files = DREAMCORE_FILES.filter(Boolean);
  if (!files.length) { errorCard(T.err_generic, "Add at least one clip."); return; }
  go.disabled = true; go.textContent = "Uploading…";
  const fd = new FormData();
  DREAMCORE_FILES.forEach((f, i) => { if (f) fd.append(`clip${String(i + 1).padStart(2, "0")}`, f, f.name); });
  fd.append("title", (D.brief || "Dreamcore").slice(0, 60));
  fd.append("brief", D.brief || "");
  fd.append("world", D.world || "");
  fd.append("bed", D.bed || "");
  fd.append("prompts", JSON.stringify(D.prompts || []));
  fd.append("slug", D.slug || S.projectSlug || "");
  try {
    const r = await fetch("/dreamcore-edit", { method: "POST", body: fd });
    const d = await r.json();
    if (d.error) throw new Error(d.error);
    startJob(d.job_id, "dreamcore");
  } catch (e) {
    go.disabled = false; go.textContent = "Cut to the music";
    errorCard(T.err_generic, String(e));
  }
}

function renderEnhanceFlow() {
  msgA("What do you want to enhance?");
  const c = card("enhance-picker");
  const grid = el("div", "mode-grid");
  [
    ["sfx", protoIcon("sound"), "SFX Master", "Add transition, reaction and word-triggered sound effects."],
    ["visual", protoIcon("visual"), "Visual Master", "Add arrows, focus cues and motion-aware visual effects."],
    ["captions", protoIcon("captions"), "Caption Master", "Generate or restyle captions for an existing video."],
    ["asmr", protoIcon("sound"), "ASMR Sound", "Make original ambience feel closer, clearer and more immersive."],
    ["actionedit", protoIcon("visual"), "Action Edit",
     "Cut three raw action clips into one finished short - beats, ramps, grade and sound."],
  ].forEach(([id, icon, title, desc]) => {
    const b = el("button", "mode-card");
    b.innerHTML = `<span class="mh"><span class="mi">${icon}</span>${esc(title)}</span><span class="md">${esc(desc)}</span>`;
    b.addEventListener("click", () => selectMode(id));
    grid.appendChild(b);
  });
  c.appendChild(grid);
  c.appendChild(btn(T.back, resetToMode, "ghost"));
  setComposer("off");
}
/* ------------------------------------------------------------------ FLOW: Action Edit

   The one enhancement that takes THREE clips instead of one. A Short in this series is
   clip1/clip2/clip3 of the same run, each carrying its own internal cut, and the editor needs
   all three together to find the beats, pick a cold open and place the ramps - so the upload
   step collects the set before anything can start. */
// The delivery a one-minute short needs: energy and momentum, without turning into a shout.
// A one-minute short is read noticeably quicker than a long explainer.
// The Sketch Explainer's defaults, stated once. They used to be "the first entry of whichever
// option list happened to load", which meant reordering a list silently changed what every new
// project produced.
const SKETCH_DEFAULTS = {
  tts_model: "bytedance/seed-speech-tts-2.0",
  tts_voice: "jess_ja_es_id_pt_en_zh",
  tts_language: "en",
  tts_native_speed: "1.14",
  tts_volume: "1.05",
  reasoning_model: "google/gemini-3.7-flash",
};
function applySketchDefaults(holder) {
  // Only fill what the user has not already chosen, so re-entering the step never overwrites a
  // deliberate pick.
  Object.keys(SKETCH_DEFAULTS).forEach(key => {
    if (holder[key] == null || holder[key] === "") holder[key] = SKETCH_DEFAULTS[key];
  });
  return holder;
}

// Matches SKETCH_DEFAULTS.tts_native_speed on purpose. Two different numbers for "how fast
// a short is read" is how the auto-bump on 9:16 silently undid the configured default.
const SHORTS_VOICE_SPEED = "1.14";
const SHORTS_VOICE_INSTRUCTION = "Upbeat, energetic and friendly. Bright, forward-leaning pace "
  + "with clear punchy emphasis on the key word of each line, a light smile in the voice, and "
  + "short confident pauses instead of drawn-out ones. Never shouty, never breathless.";

const ACTION_FILES = [null, null, null];
const ACTION_TRIMS = [null, null, null];

function actionTime(value) {
  value = Math.max(0, +value || 0);
  const minutes = Math.floor(value / 60), seconds = value - minutes * 60;
  return `${minutes}:${seconds.toFixed(1).padStart(4, "0")}`;
}

function actionTrimEditor(index, file) {
  const state = ACTION_TRIMS[index] || (ACTION_TRIMS[index] = {
    duration: 0, ranges: [], selected: 0, url: URL.createObjectURL(file)
  });
  const FRAME = 1 / 30;
  const wrap = el("div", "action-pretrim");
  const video = el("video", "action-pretrim-video");
  video.src = state.url; video.preload = "metadata"; video.muted = false;
  video.setAttribute("playsinline", "");
  const timeline = el("div", "action-pretrim-track");
  timeline.tabIndex = 0;
  timeline.setAttribute("role", "slider");
  timeline.setAttribute("aria-label", `Clip ${index + 1} pre-edit timeline`);
  const time = el("span", "action-pretrim-time", "0:00.0");
  const summary = el("span", "action-pretrim-summary", "Loading clip…");
  const rangeFields = el("div", "action-range-fields");
  const controls = el("div", "action-pretrim-controls");

  function normalise() {
    // Clamps IN PLACE. The earlier version rebuilt every range with .map, which handed back new
    // array objects - fine for a redraw, fatal for a drag, because the handle would be left
    // editing a range the state no longer contained.
    state.ranges.forEach(r => {
      r[0] = Math.max(0, Math.min(state.duration, +r[0] || 0));
      r[1] = Math.max(0, Math.min(state.duration, +r[1] || 0));
      if (r[1] < r[0]) { const swap = r[0]; r[0] = r[1]; r[1] = swap; }
    });
    const keep = state.ranges[state.selected];
    state.ranges = state.ranges.filter(r => r[1] - r[0] >= .12).sort((a, b) => a[0] - b[0]);
    const found = state.ranges.indexOf(keep);
    state.selected = found >= 0 ? found
      : Math.max(0, Math.min(state.selected, state.ranges.length - 1));
  }
  function timeAt(clientX) {
    const rect = timeline.getBoundingClientRect();
    if (!rect.width || !state.duration) return 0;
    return Math.max(0, Math.min(state.duration,
      (clientX - rect.left) / rect.width * state.duration));
  }
  function paintPlayhead() {
    const playhead = timeline.querySelector(".action-playhead");
    if (playhead && state.duration) {
      playhead.style.left = `${Math.min(100, video.currentTime / state.duration * 100)}%`;
    }
  }
  function seek(seconds) {
    if (!state.duration) return;
    video.currentTime = Math.max(0, Math.min(state.duration, seconds));
    time.textContent = actionTime(video.currentTime);
    paintPlayhead();
  }
  function keptSeconds() {
    return state.ranges.reduce((total, r) => total + r[1] - r[0], 0);
  }
  function paintSummary() {
    const kept = keptSeconds();
    summary.textContent = state.ranges.length
      ? `${actionTime(kept)} kept · ${actionTime(Math.max(0, state.duration - kept))} removed`
      : "Nothing kept — add or reset a range before building";
    wrap.classList.toggle("empty", !state.ranges.length);
  }

  // --- playback follows the TRIM, not the file -------------------------------------------
  // Play always drops the head on the first frame that survives the cut and skips the removed
  // stretches on the way through, so what you hear and see is the clip you are actually
  // handing to the editor - not the raw upload with the deleted parts still playing.
  function playFromStart() {
    if (!state.ranges.length) return;
    seek(state.ranges[0][0]);
    const started = video.play();
    if (started && started.catch) started.catch(() => {});
  }
  function togglePlay() {
    if (video.paused) playFromStart(); else video.pause();
  }
  video.addEventListener("timeupdate", () => {
    time.textContent = actionTime(video.currentTime);
    paintPlayhead();
    if (video.paused || !state.ranges.length) return;
    const at = video.currentTime;
    if (state.ranges.some(r => at >= r[0] - .03 && at <= r[1])) return;
    const next = state.ranges.find(r => r[0] > at);
    if (next) seek(next[0]);
    else { video.pause(); seek(state.ranges[0][0]); }
  });

  // --- dragging ---------------------------------------------------------------------------
  // Listeners go on the window, so a drag that leaves the track keeps working instead of
  // sticking the handle wherever the pointer happened to exit.
  function drag(onMove, onDone) {
    const move = ev => { ev.preventDefault(); onMove(ev.clientX); };
    const up = () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
      window.removeEventListener("pointercancel", up);
      if (onDone) onDone();
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
    window.addEventListener("pointercancel", up);
  }

  function renderRanges() {
    normalise(); timeline.innerHTML = ""; rangeFields.innerHTML = "";
    if (!state.duration) return;
    state.ranges.forEach((range, ri) => {
      const segment = el("div", "action-range" + (ri === state.selected ? " selected" : ""));
      segment.title = `Keep ${actionTime(range[0])}–${actionTime(range[1])}`;
      const place = () => {
        segment.style.left = `${range[0] / state.duration * 100}%`;
        segment.style.width = `${(range[1] - range[0]) / state.duration * 100}%`;
      };
      place();
      segment.addEventListener("pointerdown", ev => {
        ev.stopPropagation();
        state.selected = ri; renderRanges(); seek(range[0]);
      });
      if (ri === state.selected) {
        [["in", 0], ["out", 1]].forEach(([kind, pos]) => {
          const handle = el("i", `action-handle action-handle-${kind}`);
          handle.title = pos ? "Drag the out point" : "Drag the in point";
          handle.addEventListener("pointerdown", ev => {
            ev.preventDefault(); ev.stopPropagation();
            timeline.classList.add("dragging");
            drag(clientX => {
              // Neighbours are the limit, and a range never collapses past the 0.12 s that
              // normalise() would otherwise delete out from under the drag.
              const before = state.ranges[ri - 1], after = state.ranges[ri + 1];
              let at = timeAt(clientX);
              if (pos === 0) at = Math.min(at, range[1] - .12);
              else at = Math.max(at, range[0] + .12);
              if (pos === 0 && before) at = Math.max(at, before[1]);
              if (pos === 1 && after) at = Math.min(at, after[0]);
              range[pos] = Math.max(0, Math.min(state.duration, at));
              place(); paintSummary(); seek(range[pos]);
              const field = rangeFields.querySelectorAll("input")[pos];
              if (field) field.value = range[pos].toFixed(2);
              segment.title = `Keep ${actionTime(range[0])}–${actionTime(range[1])}`;
            }, () => { timeline.classList.remove("dragging"); renderRanges(); });
          });
          segment.appendChild(handle);
        });
      }
      timeline.appendChild(segment);
    });
    const playhead = el("i", "action-playhead");
    playhead.style.left = `${Math.min(100, video.currentTime / state.duration * 100)}%`;
    timeline.appendChild(playhead);
    paintSummary();
    const selected = state.ranges[state.selected];
    if (selected) {
      [["In", 0], ["Out", 1]].forEach(([label, pos]) => {
        const field = el("label", "action-range-field", label);
        const input = el("input"); input.type = "number"; input.min = "0";
        input.max = state.duration.toFixed(3); input.step = "0.05";
        input.value = selected[pos].toFixed(2);
        input.addEventListener("change", () => {
          selected[pos] = +input.value;
          renderRanges();
          seek(selected[pos]);        // the head follows the edit, so the frame is visible
        });
        field.appendChild(input); rangeFields.appendChild(field);
      });
    }
  }

  video.addEventListener("loadedmetadata", () => {
    state.duration = isFinite(video.duration) ? video.duration : 0;
    if (!state.ranges.length && state.duration) state.ranges = [[0, state.duration]];
    timeline.setAttribute("aria-valuemax", String(state.duration));
    renderRanges();
    if (state.ranges.length) seek(state.ranges[0][0]);
  });

  // Scrubbing: press anywhere on the track and drag. Clicking a range still selects it, because
  // that listener stops the event before it reaches here.
  timeline.addEventListener("pointerdown", ev => {
    if (!state.duration) return;
    timeline.focus();
    seek(timeAt(ev.clientX));
    const found = state.ranges.findIndex(r => r[0] <= video.currentTime && video.currentTime <= r[1]);
    if (found >= 0) { state.selected = found; renderRanges(); }
    timeline.classList.add("dragging");
    drag(clientX => seek(timeAt(clientX)), () => timeline.classList.remove("dragging"));
  });
  timeline.addEventListener("keydown", ev => {
    if (!state.duration) return;
    if (ev.key === " " || ev.key === "Spacebar") { ev.preventDefault(); togglePlay(); return; }
    if (ev.key === "Home") { ev.preventDefault(); if (state.ranges[0]) seek(state.ranges[0][0]); return; }
    if (!["ArrowLeft", "ArrowRight"].includes(ev.key)) return;
    ev.preventDefault();
    // A frame at a time by default - the old fixed 0.1 s step was three frames and made it
    // impossible to land the cut exactly. Shift jumps in half seconds for getting across a clip.
    const step = ev.shiftKey ? .5 : FRAME;
    seek(video.currentTime + (ev.key === "ArrowRight" ? step : -step));
  });

  controls.appendChild(btn("▶ Play from start", togglePlay, "ghost"));
  controls.appendChild(btn("Split at playhead", () => {
    const r = state.ranges[state.selected], at = video.currentTime;
    if (!r || at <= r[0] + .12 || at >= r[1] - .12) return;
    state.ranges.splice(state.selected, 1, [r[0], at], [at, r[1]]); renderRanges();
  }, "ghost"));
  controls.appendChild(btn("Remove selected", () => {
    if (!state.ranges.length) return;
    state.ranges.splice(state.selected, 1);
    state.selected = Math.max(0, state.selected - 1);
    renderRanges();
    if (state.ranges[state.selected]) seek(state.ranges[state.selected][0]);
  }, "ghost"));
  controls.appendChild(btn("Reset", () => {
    state.ranges = state.duration ? [[0, state.duration]] : [];
    state.selected = 0; renderRanges(); seek(0);
  }, "ghost"));
  const head = el("div", "action-pretrim-head");
  head.appendChild(el("strong", "", `Clip ${index + 1} pre-cut`)); head.appendChild(time);
  wrap.appendChild(head); wrap.appendChild(video); wrap.appendChild(timeline); wrap.appendChild(summary);
  wrap.appendChild(rangeFields); wrap.appendChild(controls);
  wrap.appendChild(el("small", "action-pretrim-hint",
    "Drag the green edges to trim · drag the track to scrub · ←/→ one frame, Shift half a second · Space plays"));
  return wrap;
}

function actionState() {
  if (!S.action) S.action = { title: "", text: "", hook: true, loop: false, ramps: "1", editorModel: "google/gemini-3.5-flash-lite" };
  return S.action;
}

function renderActionEditFlow() {
  const A = actionState();
  msgU("Action Edit");
  if (S.jobId) return;
  msgA("Drop in the three clips of one Short, remove weak scenes in the pre-cut timelines, then "
     + "let the editing agent build the strongest readable action sequence.");

  const c = card("action-edit");
  const ready = () => ACTION_FILES.filter(Boolean).length;

  const grid = el("div", "action-clip-grid");
  const go = btn("Build the edit", () => submitActionEdit(go), "primary");
  const refresh = () => {
    go.disabled = ready() < 3;
    go.textContent = ready() >= 3 ? "Build the edit" : `Build the edit (${ready()}/3 clips)`;
  };

  [0, 1, 2].forEach(i => {
    const column = el("div", "action-clip-column");
    const drop = el("div", "aicore-drop");
    const setName = (f) => {
      drop.classList.toggle("filled", !!f);
      const size = f && (f.size >= 1048576
        ? (f.size / 1048576).toFixed(1) + " MB"
        : Math.max(1, Math.round(f.size / 1024)) + " KB");
      drop.innerHTML = f
        ? `<b>${esc(f.name)}</b><small>Clip ${i + 1} · ${size} · click to replace</small>`
        : `<b>Clip ${i + 1}</b><small>drop it here, or click to choose</small>`;
    };
    setName(ACTION_FILES[i]);
    // Clearing a slot needed a way out: the drop zone only ever replaced, so a clip added by
    // mistake could not be taken back off - you had to reload and start the whole set again.
    const clear = el("button", "action-clip-remove");
    clear.type = "button";
    clear.innerHTML = "&#10005;";
    clear.title = `Remove clip ${i + 1}`;
    clear.setAttribute("aria-label", `Remove clip ${i + 1}`);
    const showClear = () => { clear.hidden = !ACTION_FILES[i]; };
    clear.addEventListener("click", ev => {
      ev.stopPropagation();
      const trim = ACTION_TRIMS[i];
      if (trim) {
        // The <video> keeps reading from the blob, so it has to be let go before the URL is
        // revoked or Chrome holds the whole file in memory for the rest of the session.
        const view = column.querySelector(".action-pretrim-video");
        if (view) { try { view.pause(); } catch (e) {} view.removeAttribute("src"); view.load(); }
        if (trim.url) URL.revokeObjectURL(trim.url);
      }
      ACTION_FILES[i] = null; ACTION_TRIMS[i] = null;
      const editor = column.querySelector(".action-pretrim"); if (editor) editor.remove();
      setName(null); showClear(); refresh();
    });
    const take = (f) => {
      if (!f) return;
      if (ACTION_TRIMS[i] && ACTION_TRIMS[i].url) URL.revokeObjectURL(ACTION_TRIMS[i].url);
      ACTION_FILES[i] = f; ACTION_TRIMS[i] = null; setName(f);
      const old = column.querySelector(".action-pretrim"); if (old) old.remove();
      column.appendChild(actionTrimEditor(i, f)); showClear(); refresh();
    };
    drop.addEventListener("click", () => pickFile("video/*,.mp4,.mov,.webm", take));
    drop.addEventListener("dragover", e => { e.preventDefault(); drop.classList.add("over"); });
    drop.addEventListener("dragleave", () => drop.classList.remove("over"));
    drop.addEventListener("drop", e => {
      e.preventDefault(); drop.classList.remove("over");
      take((e.dataTransfer.files || [])[0]);
    });
    column.appendChild(clear);
    column.appendChild(drop);
    if (ACTION_FILES[i]) column.appendChild(actionTrimEditor(i, ACTION_FILES[i]));
    showClear();
    grid.appendChild(column);
  });
  c.appendChild(grid);

  c.appendChild(textField("Title", A.title, "Downhill Skateboard",
    v => { A.title = v; persist(); }));
  c.appendChild(textField("Text overlay (optional)", A.text, "he sent it",
    v => { A.text = v; persist(); },
    "3-5 words. Shown from 0.4s to 2.4s, near the top, clear of the platform's own buttons."));
  c.appendChild(selectField("Speed ramps", [
    { value: "2", label: "Two - on the strongest trick peaks" },
    { value: "1", label: "One - only the single biggest peak" },
    { value: "0", label: "None - keep it all at full speed" },
  ], A.ramps, v => { A.ramps = v; persist(); }));
  c.appendChild(selectField("Editing brain", [
    { value: "google/gemini-3.5-flash-lite", label: "AI editor · Gemini 3.5 Flash Lite" },
    { value: "google/gemini-3.1-flash-lite", label: "AI editor · Gemini 3.1 Flash Lite" },
    { value: "openai/gpt-5.6-luna", label: "AI editor · GPT-5.6 Luna" },
    { value: "openai/gpt-5.6-terra", label: "AI editor · GPT-5.6 Terra" },
    { value: "openai/gpt-5.6-sol", label: "AI editor · GPT-5.6 Sol" },
    { value: "local", label: "Local cut engine · no API" },
  ], A.editorModel || "google/gemini-3.5-flash-lite", v => { A.editorModel = v; persist(); }));
  c.appendChild(el("div", "card-note",
    "The agent sees a contact sheet of the actual clips and decides the hook, story order and ramps. "
    + "It never adds library SFX."));
  c.appendChild(toggleField("Cold open", A.hook !== false, v => { A.hook = v; persist(); }));
  c.appendChild(toggleField("Loop shaping", A.loop === true, v => { A.loop = v; persist(); }));
  c.appendChild(el("div", "card-note",
    "Cuts land on movement. The original action sound stays intact, with no sounds taken from "
    + "your SFX library. The series grade and a -14 LUFS master run locally."));

  const foot = el("div", "card-foot");
  foot.appendChild(btn(T.back, resetToMode, "ghost"));
  foot.appendChild(el("span", "spacer"));
  foot.appendChild(go);
  c.appendChild(foot);
  refresh();
  setComposer("off");
}

async function submitActionEdit(go) {
  const A = actionState();
  if (ACTION_FILES.filter(Boolean).length < 3) {
    errorCard(T.err_generic, "Action Edit needs all three clips of the Short.");
    return;
  }
  go.disabled = true; go.textContent = "Uploading…";
  const fd = new FormData();
  // Named clip1/clip2/clip3: the order is the story order and the server reads them by name,
  // never by whatever order the parts happen to arrive in.
  ACTION_FILES.forEach((f, i) => fd.append(`clip${i + 1}`, f, f.name));
  fd.append("title", A.title || "Action Edit");
  fd.append("overlay_text", A.text || "");
  fd.append("max_ramps", A.ramps || "2");
  fd.append("editor_model", A.editorModel || "google/gemini-3.5-flash-lite");
  const trimPlan = {};
  ACTION_TRIMS.forEach((state, index) => {
    if (state) trimPlan[String(index + 1)] = state.ranges || [];
  });
  const empty = ACTION_TRIMS.findIndex(state => state && (!state.ranges || !state.ranges.length));
  if (empty >= 0) {
    go.disabled = false; go.textContent = "Build the edit";
    errorCard(T.err_generic, `Clip ${empty + 1} has no footage left. Keep at least one range or reset it.`);
    return;
  }
  fd.append("trim_plan", JSON.stringify(trimPlan));
  if (A.hook !== false) fd.append("hook_enabled", "on");
  if (A.loop === true) fd.append("loop_shaping", "on");
  try {
    const r = await fetch("/action-edit-run", { method: "POST", body: fd });
    const jid = new URL(r.url, location.href).searchParams.get("id");
    if (!jid) throw new Error("no job id");
    startJob(jid);
  } catch (e) {
    go.disabled = false; go.textContent = "Build the edit";
    errorCard(T.upload_failed, String(e));
  }
}

function visibleOutputFields() {
  return isCultureFacts()
    ? OUTPUT_FIELDS.filter(([k]) => ["out_transition_sfx", "out_captions"].includes(k))
    : OUTPUT_FIELDS;
}
function outputsSummary() {
  const on = visibleOutputFields().filter(([k]) => S.values[k]).map(([, l]) => l);
  let s = on.join(", ") || "No outputs";
  if (S.values.halt_after_speech) s += " · " + T.halt_after_speech;
  s += " · " + T.sfx_amount + ": " + (S.values.sfx_amount || "medium");
  return s;
}

function renderReviewCard() {
  const c = card();   // the "REVIEW / Ready to create" title comes from the step header
  if (isOthersVsKing()) {
    const action = String(S.values.others_action || "").trim() || "Agent chooses a fresh action";
    const specs = el("div", "review-specs");
    [["Format", "Others doing X"], ["Action", action],
     ["Search", "TikTok + Instagram · relevance, likes and views"],
     ["Edit", "Several clear attempts → one reserved astonishing payoff"],
     ["Reasoning model", labelFor(OPT.reasoning_model, S.values.reasoning_model)]].forEach(([k, v]) => {
      const tile = el("div", "review-spec");
      tile.appendChild(el("span", "rs-k", esc(k)));
      tile.appendChild(el("span", "rs-v", esc(v)));
      specs.appendChild(tile);
    });
    c.appendChild(specs);
    const foot = el("div", "card-foot");
    foot.appendChild(el("span", "spacer"));
    foot.appendChild(btn("⚡ Build comparison", submitRun, "primary review-cta"));
    c.appendChild(foot);
    return;
  }
  const rows = [
    ["Mode", isCultureFacts() ? "Clip Short" : isVisualsFromScript() ? "AI Short" : T.mode_script_t, null],
    ["Visual source", isCultureFacts()
      ? `${(S.values.v4_tiktok_discovery_provider || "scrapedo") === "scrapedo" ? "Scrape.do" : "Bright Data"} · Scrape ${(S.values.scraping_engine || "v4").toUpperCase()}`
      : (S.values.clip_source === "scrape" ? T.src_scrape : T.src_generate),
      isCultureFacts() ? null : "source"],
  ];
  if (S.values.clip_source === "scrape") {
    if (!isCultureFacts()) rows.push([T.scrape_engine,
      `Scrape ${(S.values.scraping_engine || "v4").toUpperCase()}`, "source"]);
    rows.push([T.script_relevancy, (S.values.script_relevancy || "90") + "%", isCultureFacts() ? null : "source"]);
    rows.push(["Search platforms", (S.values.scrape_platforms || "tiktok,x,instagram").split(",").map(p => p === "x" ? "X" : p.charAt(0).toUpperCase() + p.slice(1)).join(", "), "source"]);
    if (S.values.scrape_terms) rows.push(["Search terms", S.values.scrape_terms, isCultureFacts() ? null : "source"]);
    if (S.values.scraping_engine === "v4") rows.push([
      "TikTok discovery",
      (S.values.v4_tiktok_discovery_provider || "scrapedo") === "scrapedo" ? "Scrape.do rendered search" : "Bright Data dataset",
      "source"
    ]);
  } else {
    rows.push([T.video_model, labelFor(OPT.video_model, S.values.video_model), "source"]);
    rows.push([T.image_model, labelFor(OPT.image_model, S.values.image_model), "source"]);
  }
  rows.push(["Reasoning model", labelFor(OPT.reasoning_model, S.values.reasoning_model), "reasoning"]);
  rows.push(["Voice", S.values.tts_voice + " · " + labelFor(OPT.tts_model, S.values.tts_model), "script"]);
  if (!isCultureFacts()) rows.push(["Speaker video", S.values.enable_speaker_hook ? "On" : "Off", "script"]);
  rows.push(["Hook", S.values.hook_text ? T.hook_marked : T.no_hook, "script"]);
  if (S.values.impact_word) rows.push(["Impact word", S.values.impact_word, "script"]);
  var vfxLayers = [S.values.add_visual_effects !== false ? "arrows" : null].filter(Boolean);
  rows.push([T.vfx_amount, (vfxLayers.length ? vfxLayers.join(", ") + " · " : "off · ") + (S.values.vfx_amount || "medium"), "outputs"]);
  rows.push([T.sfx_amount, S.values.sfx_amount || "medium", "outputs"]);
  // spec tiles - one card per setting, with an inline Edit that jumps to the step
  const specs = el("div", "review-specs");
  rows.forEach(([k, v, editKey]) => {
    const tile = el("div", "review-spec");
    tile.appendChild(el("span", "rs-k", esc(k)));
    tile.appendChild(el("span", "rs-v", esc(v)));
    if (editKey) { const b = el("button", "rs-edit", esc(T.edit)); b.addEventListener("click", () => editStep(editKey)); tile.appendChild(b); }
    specs.appendChild(tile);
  });
  c.appendChild(specs);
  // (no "Outputs" section here — the layers are already chosen in the Finish step)
  if (S.projectSlug) {
    c.appendChild(el("div", "card-note", esc(T.project_loaded) + ": " + esc(S.projectTitle || S.projectSlug)
      + " · " + esc(labelForRunMode(S.values.loaded_project_mode))));
  }
  const foot = el("div", "card-foot");
  foot.appendChild(el("span", "spacer"));
  foot.appendChild(btn("⚡ " + T.create_short, submitRun, "primary review-cta"));
  c.appendChild(foot);
}

/* ------------------------------------------------------------------ payload adapter (/run) */
function buildRunForm() {
  const fd = new FormData();
  const A = MAN.run.always;
  Object.keys(A).forEach(k => fd.append(k, A[k]));
  MAN.run.state_hidden.forEach(k => fd.append(k, S.values[k] ? "on" : ""));
  MAN.run.text.forEach(k => fd.append(k, S.values[k] != null ? String(S.values[k]) : ""));
  MAN.run.check.forEach(k => { if (S.values[k]) fd.append(k, "on"); });
  if (isDiscovery() && S.values.discovery_keep_original_audio) {
    fd.append("discovery_keep_original_audio", "on");
  }
  if (FILES.speaker_image_file) fd.append("speaker_image_file", FILES.speaker_image_file);
  if (FILES.motion_loop_first_frame) fd.append("motion_loop_first_frame", FILES.motion_loop_first_frame);
  return fd;
}
async function submitRun() {
  const c = card(); c.appendChild(el("div", "dots", "<i></i><i></i><i></i>"));
  try {
    const r = await fetch("/run", { method: "POST", body: buildRunForm() });
    const jid = new URL(r.url, location.href).searchParams.get("id");
    if (!jid) throw new Error("no job id");
    startJob(jid);
  } catch (e) {
    c.remove(); errorCard(T.err_generic, String(e));
  }
}

/* ------------------------------------------------------------------ FLOW: masters (sfx/visual/captions) */
function renderMasterFlow(kind) {
  const titles = { sfx: T.mode_sfx_t, visual: T.mode_vfx_t, captions: T.mode_captions_t,
    asmr: "ASMR Sound" };
  const uploadQ = { sfx: T.sfx_upload_q, visual: T.vfx_upload_q, captions: T.cap_upload_q,
    asmr: "Upload a video with original ambience. Its picture stays untouched while the sound is mastered." };
  msgU(esc(titles[kind]));
  msgA(esc(uploadQ[kind]));
  const f = FILES.master_video;
  if (S.completed.includes("upload") && (f || S._uploadName)) {
    msgU("🎞 " + esc(f ? f.name : S._uploadName), "upload");
  } else if (S.step === "upload") {
    const c = card();
    const drop = el("div", "upl-drop", prototypeMode
      ? `<span class="proto-upload-icon">${protoIcon("visual")}</span><b>${esc(T.attach_video)}</b><small>MP4 · MOV · WebM</small>`
      : "📎 " + esc(T.attach_video) + "<br><small>MP4 · MOV · WebM</small>");
    drop.tabIndex = 0; drop.setAttribute("role", "button");
    const accept = "video/mp4,video/quicktime,video/webm,video/x-matroska,.mp4,.mov,.webm,.mkv";
    const onFile = (file) => {
      if (!file) return;
      FILES.master_video = file; S._uploadName = file.name;
      completeStep("upload", "settings");
    };
    drop.addEventListener("click", () => pickFile(accept, onFile));
    drop.addEventListener("keydown", e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); drop.click(); } });
    dragDrop(drop, onFile);
    c.appendChild(drop);
    setComposer("upload", onFile, accept);
    return;
  }

  if (S.completed.includes("upload") && S.step === "settings" && !S.jobId) {
    const c = card();
    if (f) {
      const v = el("video", "upl-preview-video"); v.controls = true; v.muted = true;
      v.src = URL.createObjectURL(f); c.appendChild(v);
    } else {
      c.appendChild(el("div", "card-note", "⚠ " + esc(S._uploadName || "") +
        " - the file selection was lost on reload. Please attach it again."));
      c.appendChild(btn("📎 " + T.attach_video, () => editStep("upload"), "ghost small"));
    }
    if (kind === "sfx") {
      c.appendChild(selectField(T.planning_agent, OPT.master_reasoning, S.master.reasoning_model,
        v => { const changed=!!S.master.reasoning_model&&S.master.reasoning_model!==v; S.master.reasoning_model = v; S.master.reasoning_mode = reasoningOptions(v, S.master.reasoning_mode).value; if(changed)setTimeout(renderAll,0); }));
      appendReasoningField(c, S.master.reasoning_model || firstVal(OPT.master_reasoning), S.master);
      c.appendChild(amountSliderField(T.sfx_amount, S.master.sfx_amount || "medium", v => S.master.sfx_amount = v));
    } else if (kind === "visual") {
      c.appendChild(selectField(T.analysis_agent, OPT.vfx_reasoning, S.master.reasoning_model,
        v => { const changed=!!S.master.reasoning_model&&S.master.reasoning_model!==v; S.master.reasoning_model = v; S.master.reasoning_mode = reasoningOptions(v, S.master.reasoning_mode).value; if(changed)setTimeout(renderAll,0); }));
      appendReasoningField(c, S.master.reasoning_model || firstVal(OPT.vfx_reasoning), S.master);
      c.appendChild(amountSliderField(T.effect_amount, S.master.vfx_amount || "medium", v => S.master.vfx_amount = v));
    } else if (kind === "captions") {
      c.appendChild(selectField(T.cap_max_words, OPT.caption_max_words, S.master.caption_max_words,
        v => S.master.caption_max_words = v));
      c.appendChild(selectField(T.cap_center_y, OPT.caption_center_y, S.master.caption_center_y,
        v => S.master.caption_center_y = v));
    } else {
      c.appendChild(selectField("Sound character", [
        { value: "close", label: "Close-up ASMR - vivid texture and movement" },
        { value: "natural", label: "Natural detail - clear but realistic" },
        { value: "soft", label: "Soft & calm - gentle mechanical ambience" },
      ], S.master.asmr_profile || "close", v => S.master.asmr_profile = v));
      c.appendChild(el("div", "card-note", "Uses only the video's original sound. No AI generation, music or unrelated effects are added."));
    }
    const labels = { sfx: "🔊 " + T.add_sfx, visual: "➜ " + T.add_arrows,
      captions: "💬 " + T.add_captions, asmr: "Master ASMR sound" };
    const foot = el("div", "card-foot");
    foot.appendChild(btn(T.back, () => editStep("upload"), "ghost"));
    foot.appendChild(el("span", "spacer"));
    const go = btn(labels[kind], () => submitMaster(kind), "primary");
    if (!f) go.disabled = true;
    foot.appendChild(go);
    c.appendChild(foot);
  }
  setComposer("off");
}
async function submitMaster(kind) {
  const man = MAN.masters[kind];
  const fd = new FormData();
  fd.append(man.file, FILES.master_video);
  if (kind === "sfx") {
    fd.append("reasoning_model", S.master.reasoning_model || firstVal(OPT.master_reasoning));
    fd.append("reasoning_mode", S.master.reasoning_mode || "");
    fd.append("sfx_amount", S.master.sfx_amount || "medium");
  } else if (kind === "visual") {
    fd.append("reasoning_model", S.master.reasoning_model || firstVal(OPT.vfx_reasoning));
    fd.append("reasoning_mode", S.master.reasoning_mode || "");
    fd.append("vfx_amount", S.master.vfx_amount || "medium");
  } else if (kind === "captions") {
    fd.append("caption_max_words", S.master.caption_max_words || firstVal(OPT.caption_max_words) || "4");
    fd.append("caption_center_y", S.master.caption_center_y || firstVal(OPT.caption_center_y) || "0.62");
  } else {
    fd.append("asmr_profile", S.master.asmr_profile || "close");
  }
  const c = card(); c.appendChild(el("div", "dots", "<i></i><i></i><i></i>"));
  try {
    const r = await fetch(man.action, { method: "POST", body: fd });
    const jid = new URL(r.url, location.href).searchParams.get("id");
    if (!jid) throw new Error("no job id");
    // All Master runs (SFX / VFX / captions) show their progress INSIDE the chat shell, so the
    // sidebar stays visible - same as every other tab, just the progress interface instead of chat.
    startJob(jid);
  } catch (e) { c.remove(); errorCard(T.upload_failed, String(e)); }
}

/* ------------------------------------------------------------------ FLOW: reddit story */
/* ------------------------------------------------------------------ physics simulation flow
   One scene, one value swept, rendered in Blender. No model call anywhere in this flow -
   the preset and the values fully determine the output, so there is nothing to wait on
   but the renderer. */
const PHYS_BRIEF_MODELS = [
  { id: "google/gemini-3.5-flash-lite", t: "Gemini 3.5 Flash Lite (fast)" },
  { id: "google/gemini-3.1-flash-lite", t: "Gemini 3.1 Flash Lite" },
  { id: "google/gemini-3.5-flash", t: "Gemini 3.5 Flash" },
  { id: "deepseek/deepseek-v4-flash-0731", t: "DeepSeek V4 Flash (cheapest)" },
  { id: "deepseek/deepseek-v4-pro", t: "DeepSeek V4 Pro" },
  { id: "anthropic/claude-opus-4.8", t: "Claude Opus 4.8 (most detailed)" },
];
/* The scene library, read from the scene FILES at /physics-scenes. Nothing about the
   simulations is written down here: the app used to name three presets by hand, two of
   which no longer existed and nine of which were missing, so the newest scene was always
   the one you could not reach. A scene added to physics_mode/scenes/lib/ now appears in
   this list on the next page load. */
let PHYS_LIB = null, PHYS_LIB_ERR = "", physLibPending = false;
function physFetchLibrary(then) {
  if (physLibPending) return;
  physLibPending = true;
  fetch("/physics-scenes").then(r => r.json()).then(d => {
    PHYS_LIB = d.scenes || []; PHYS_LIB_ERR = d.error || "";
  }).catch(e => { PHYS_LIB = []; PHYS_LIB_ERR = String(e); })
    .then(() => { physLibPending = false; then && then(); });
}
const PHYS_QUALITY = [
  { s: 12, t: "Draft", d: "grainy, quickest" },
  { s: 24, t: "Standard", d: "what the finished shorts use" },
  { s: 48, t: "Sharp", d: "clean, roughly twice the wait" },
];
function physState() {
  if (!S.physics) {
    S.physics = { scene: "", params: {}, sweep: true, sweepValues: "",
                  seconds: 0, samples: 24, prompt: "" };
  }
  return S.physics;
}
// Scene keys are snake_case; only the first letter is raised. CSS capitalize would title-case
// every word, which turns "ball mass per take (kg)" into "(Kg)".
function physCap(s) {
  const t = String(s || "").replace(/_/g, " ");
  return t.charAt(0).toUpperCase() + t.slice(1);
}
function physSceneOf(P) {
  return (PHYS_LIB || []).find(s => s.scene === P.scene) || null;
}
function physTakeCount(sc, P) {
  if (!sc || !sc.sweep || !P.sweep) return 1;
  const v = physSweepValues(sc, P);
  return v.length || (sc.sweep.values || []).length || 1;
}
function physSweepValues(sc, P) {
  const raw = String(P.sweepValues || "").trim();
  if (!raw) return (sc && sc.sweep ? sc.sweep.values : []) || [];
  return raw.split(/[,;\s]+/).map(Number).filter(n => isFinite(n));
}
/* ------------------------------------------------------------------ low-poly story short
   Crude PS1-era 3D, written end to end by the models: story and shot list, then one whole
   Blender script per shot. Looking cheap is the point, so nothing here is hand-authored. */
function renderLowpolyFlow() {
  msgU(esc(T.mode_lowpoly_t));
  msgA(esc(T.lowpoly_intro));
  if (S.jobId) return;
  const c = card();
  const row = el("div", "field");
  row.innerHTML = `<label>${esc(T.lowpoly_prompt_label)}</label>`;
  const ta = el("textarea", "");
  ta.rows = 4;
  ta.placeholder = T.lowpoly_prompt_ph;
  ta.value = S.values.lp_prompt || "";
  ta.addEventListener("input", () => { S.values.lp_prompt = ta.value; persist(); });
  row.appendChild(ta);
  row.appendChild(el("div", "hint", esc(T.lowpoly_hint)));
  c.appendChild(row);
  const lenRow = el("div", "field");
  lenRow.innerHTML = `<label>${esc(T.lowpoly_len)}</label>`;
  const sel = el("select", "");
  [20, 30, 45, 60].forEach(v => {
    const o = el("option", "", v + "s");
    o.value = String(v);
    if (String(S.values.lp_seconds || 30) === String(v)) o.selected = true;
    sel.appendChild(o);
  });
  sel.addEventListener("change", () => { S.values.lp_seconds = sel.value; persist(); });
  lenRow.appendChild(sel);
  c.appendChild(lenRow);
  const go = btn(T.lowpoly_go, async () => {
    if (!(S.values.lp_prompt || "").trim()) {
      errorCard(T.err_generic, T.lowpoly_missing); return;
    }
    go.disabled = true; go.textContent = "…";
    try {
      const d = await jpost("/lowpoly-run", {
        prompt: S.values.lp_prompt || "",
        seconds: Number(S.values.lp_seconds || 30),
      });
      if (d.error) throw new Error(d.error);
      startJob(d.job_id, "lowpoly");
    } catch (e) {
      go.disabled = false; go.textContent = T.lowpoly_go;
      errorCard(T.err_generic, String(e));
    }
  }, "primary");
  c.appendChild(go);
  setComposer("off");
}

/* ---- step 1: which simulation ---------------------------------------------------- */
function physRenderGrid(host, P) {
  host.innerHTML = "";
  if (!PHYS_LIB) {
    host.appendChild(el("div", "phys-loading", "<i></i><i></i><i></i>"));
    return;
  }
  if (!PHYS_LIB.length) {
    host.appendChild(el("div", "hint",
      esc(PHYS_LIB_ERR || "No scenes found in physics_mode/scenes/lib.")));
    return;
  }
  PHYS_LIB.forEach(sc => {
    const on = P.scene === sc.scene;
    const b = el("button", "phys-tile" + (on ? " on" : ""));
    const takes = sc.sweep ? (sc.sweep.values || []).length : 0;
    const kind = takes > 1 ? `${takes} takes` : (sc.loop ? "loops" : "one take");
    const sweepLine = sc.sweep
      ? `${sc.sweep.param.replace(/_/g, " ")} · ${(sc.sweep.values || []).join(" · ")}${sc.sweep.unit || ""}`
      : "";
    b.innerHTML =
      `<span class="pt-head"><strong>${esc(sc.title)}</strong>` +
      `<em class="pt-kind${takes > 1 ? " sweep" : ""}">${esc(kind)}</em></span>` +
      `<p>${esc(sc.does)}</p>` +
      `<span class="pt-foot">${sweepLine ? `<code>${esc(sweepLine)}</code>` : "<code></code>"}` +
      `<i>${sc.params.length} setting${sc.params.length === 1 ? "" : "s"}</i></span>`;
    b.addEventListener("click", () => {
      P.scene = on ? "" : sc.scene;
      P.params = {};
      P.sweepValues = "";
      P.seconds = 0;
      if (P.scene) P.prompt = "";
      renderAll(); persist();
    });
    host.appendChild(b);
  });
}

/* ---- step 2: the knobs of the chosen scene ----------------------------------------- */
function physParamControl(spec, P) {
  const val = P.params[spec.key] !== undefined ? P.params[spec.key] : spec.default;
  const row = el("div", "phys-param");
  const head = el("div", "pp-head");
  head.innerHTML = `<span>${esc(physCap(spec.label))}</span>`;
  row.appendChild(head);
  if (spec.choices && spec.choices.length) {
    const seg = el("div", "phys-seg");
    spec.choices.forEach(ch => {
      const b = el("button", "phys-seg-b" + (String(ch) === String(val) ? " on" : ""),
                   esc(physCap(ch)));
      b.addEventListener("click", () => {
        P.params[spec.key] = ch; renderAll(); persist();
      });
      seg.appendChild(b);
    });
    row.appendChild(seg);
  } else if (spec.range && spec.range.length === 2) {
    const [lo, hi] = spec.range.map(Number);
    const whole = Number.isInteger(lo) && Number.isInteger(hi) && Number.isInteger(Number(spec.default));
    const wrap = el("div", "phys-slide");
    const inp = el("input", "");
    inp.type = "range"; inp.min = String(lo); inp.max = String(hi);
    inp.step = whole ? "1" : String(Math.max(0.01, (hi - lo) / 200));
    inp.value = String(val);
    const out = el("output", "", String(val));
    inp.addEventListener("input", () => {
      const n = whole ? Math.round(Number(inp.value)) : Math.round(Number(inp.value) * 100) / 100;
      P.params[spec.key] = n; out.textContent = String(n); persist();
    });
    wrap.append(inp, out);
    if (spec.key === "seed") {
      const dice = el("button", "phys-dice", "new");
      dice.title = "A different seed is a visibly different take";
      dice.addEventListener("click", () => {
        const n = Math.floor(Math.random() * (hi - lo + 1)) + lo;
        P.params[spec.key] = n; inp.value = String(n); out.textContent = String(n); persist();
      });
      wrap.appendChild(dice);
    }
    row.appendChild(wrap);
  } else {
    return null;                       // free-form parameter: leave it at its default
  }
  if (spec.note) {
    const n = el("p", "pp-note", esc(spec.note));
    row.appendChild(n);
  }
  return row;
}

function physShotCard(P) {
  const sc = physSceneOf(P);
  const c = card("phys-shot");
  if (sc) {
    const head = el("div", "phys-shot-head");
    head.innerHTML = `<strong>${esc(sc.title)}</strong><p>${esc(sc.does)}</p>`;
    c.appendChild(head);

    // Format. A scene that declares a sweep can be either the comparison - the same setup
    // once per value, joined - or a single event.
    if (sc.sweep) {
      const f = el("div", "phys-param");
      f.appendChild(el("div", "pp-head", "<span>Format</span>"));
      const seg = el("div", "phys-seg");
      [[true, "Comparison"], [false, "Single take"]].forEach(([on, label]) => {
        const b = el("button", "phys-seg-b" + (P.sweep === on ? " on" : ""), label);
        b.addEventListener("click", () => { P.sweep = on; renderAll(); persist(); });
        seg.appendChild(b);
      });
      f.appendChild(seg);
      f.appendChild(el("p", "pp-note", P.sweep
        ? `The same setup rendered once per value and joined, each take labelled. The contrast IS the video.`
        : `One event, rendered once. ${esc(physCap(sc.sweep.param))} stays at its default.`));
      c.appendChild(f);
      if (P.sweep) {
        const vr = el("div", "phys-param");
        vr.appendChild(el("div", "pp-head",
          `<span>${esc(physCap(sc.sweep.param))} per take${sc.sweep.unit ? " (" + esc(sc.sweep.unit) + ")" : ""}</span>`));
        const inp = el("input", "phys-values");
        inp.type = "text";
        inp.value = P.sweepValues || (sc.sweep.values || []).join(", ");
        inp.addEventListener("input", () => { P.sweepValues = inp.value; persist(); });
        vr.appendChild(inp);
        vr.appendChild(el("p", "pp-note",
          "Comma separated. Three or four reads best - each one is a full render."));
        c.appendChild(vr);
      }
    }

    // Everything the scene declares, minus the value being swept (that one is the format).
    const swept = P.sweep && sc.sweep ? sc.sweep.param : "";
    // Declaration order, except the seed - the scene files put it first, but it is the
    // "give me another one of these" knob, not the first decision about the shot.
    const knobs = sc.params.filter(p => p.key !== swept)
                           .sort((a, b) => (a.key === "seed") - (b.key === "seed"));
    const main = knobs.slice(0, 4), rest = knobs.slice(4);
    main.forEach(spec => { const r = physParamControl(spec, P); if (r) c.appendChild(r); });
    if (rest.length) {
      const more = el("details", "phys-more");
      more.appendChild(el("summary", "", `${rest.length} more setting${rest.length === 1 ? "" : "s"}`));
      rest.forEach(spec => { const r = physParamControl(spec, P); if (r) more.appendChild(r); });
      c.appendChild(more);
    }
  } else {
    const head = el("div", "phys-shot-head");
    head.innerHTML = `<strong>The app picks the scene</strong>` +
      `<p>${esc(P.prompt)}</p>`;
    c.appendChild(head);
    const m = el("div", "phys-param");
    m.appendChild(el("div", "pp-head", `<span>${esc(T.physics_brief_model)}</span>`));
    const sel = el("select", "");
    PHYS_BRIEF_MODELS.forEach(mo => {
      const o = el("option", "", esc(mo.t));
      o.value = mo.id;
      if ((S.values.phys_brief_model || PHYS_BRIEF_MODELS[0].id) === mo.id) o.selected = true;
      sel.appendChild(o);
    });
    sel.addEventListener("change", () => { S.values.phys_brief_model = sel.value; persist(); });
    m.appendChild(sel);
    m.appendChild(el("p", "pp-note",
      "Only used if no scene in the library fits, in which case one is written from scratch. "
      + "The library is tried first, always."));
    c.appendChild(m);
  }

  // Length and quality apply to both paths.
  const secDefault = sc && sc.seconds ? Number(sc.seconds) : 4;
  const lr = el("div", "phys-param");
  lr.appendChild(el("div", "pp-head", "<span>Seconds per take</span>"));
  const wrap = el("div", "phys-slide");
  const sl = el("input", "");
  sl.type = "range"; sl.min = "2"; sl.max = "12"; sl.step = "0.5";
  sl.value = String(P.seconds || secDefault);
  const out = el("output", "", String(P.seconds || secDefault) + "s");
  sl.addEventListener("input", () => {
    P.seconds = Number(sl.value); out.textContent = sl.value + "s"; persist();
  });
  wrap.append(sl, out);
  lr.appendChild(wrap);
  c.appendChild(lr);

  const qr = el("div", "phys-param");
  qr.appendChild(el("div", "pp-head", "<span>Render quality</span>"));
  const qseg = el("div", "phys-seg");
  PHYS_QUALITY.forEach(q => {
    const b = el("button", "phys-seg-b" + (Number(P.samples) === q.s ? " on" : ""), esc(q.t));
    b.title = q.d;
    b.addEventListener("click", () => { P.samples = q.s; renderAll(); persist(); });
    qseg.appendChild(b);
  });
  qr.appendChild(qseg);
  c.appendChild(qr);

  const takes = physTakeCount(sc, P);
  const secs = P.seconds || secDefault;
  const note = el("div", "phys-estimate");
  note.innerHTML = `<span>${takes} × ${secs}s</span>` +
    `<em>One cheap frame is rendered first — nothing long starts until you approve it.</em>`;
  c.appendChild(note);

  const foot = el("div", "card-foot");
  foot.appendChild(btn(T.back, () => editStep("scene"), "ghost"));
  const go = btn("Render the preview frame", async () => {
    go.disabled = true; go.textContent = "…";
    try {
      const body = { seconds: secs, samples: P.samples || 24 };
      if (P.scene) {
        body.scene = P.scene;
        body.params = P.params;
        body.sweep = !!P.sweep;
        if (sc && sc.sweep && P.sweep) body.sweep_values = physSweepValues(sc, P);
      } else {
        body.prompt = P.prompt || "";
        body.brief_model = S.values.phys_brief_model || PHYS_BRIEF_MODELS[0].id;
      }
      const d = await jpost("/physics-run", body);
      if (d.error) throw new Error(d.error);
      startJob(d.job_id, "physics");
    } catch (e) {
      go.disabled = false; go.textContent = "Render the preview frame";
      errorCard(T.err_generic, String(e));
    }
  }, "primary");
  foot.appendChild(go);
  c.appendChild(foot);
}

function renderPhysicsFlow() {
  const P = physState();
  msgU(esc(T.mode_physics_t));
  if (S.jobId) return;
  if (PHYS_LIB === null) physFetchLibrary(() => renderAll());

  if (S.step === "shot" && (P.scene || (P.prompt || "").trim())) {
    // The knobs come from the scene file, so there is nothing to draw until it is here.
    if (P.scene && !PHYS_LIB) {
      card("phys-pick").appendChild(el("div", "phys-loading", "<i></i><i></i><i></i>"));
      setComposer("off");
      return;
    }
    const picked = physSceneOf(P);
    if (P.scene && !picked) {          // scene renamed or removed since it was chosen
      P.scene = ""; P.params = {};
      goto("scene");
      return;
    }
    msgU(esc(picked ? picked.title : P.prompt), "scene");
    msgA("Set the shot. Every number here is one the scene was built to accept.");
    physShotCard(P);
    setComposer("off");
    return;
  }

  msgA(esc(T.physics_intro));
  const c = card("phys-pick");
  const grid = el("div", "phys-grid");
  c.appendChild(grid);
  physRenderGrid(grid, P);

  const or = el("div", "phys-or");
  or.innerHTML = `<span>or describe it and let the app choose</span>`;
  c.appendChild(or);
  const ta = el("textarea", "phys-prompt");
  ta.rows = 2;
  ta.placeholder = T.physics_prompt_ph;
  ta.value = P.prompt || "";
  ta.addEventListener("input", () => {
    P.prompt = ta.value;
    if (ta.value.trim() && P.scene) { P.scene = ""; renderAll(); }
    persist();
  });
  c.appendChild(ta);

  const foot = el("div", "card-foot");
  foot.appendChild(btn(T.back, resetToMode, "ghost"));
  const next = btn(T.continue, () => {
    if (!P.scene && !(P.prompt || "").trim()) {
      errorCard(T.err_generic, "Pick a simulation or describe one.");
      return;
    }
    completeStep("scene", "shot");
  }, "primary");
  foot.appendChild(next);
  c.appendChild(foot);
  setComposer("off");
}

function renderRedditFlow() {
  msgU(esc(T.mode_reddit_t));
  msgA(esc(T.reddit_intro));
  if (!S.completed.includes("discover")) {
    if (S.step === "discover") {
      const c = card();
      const b = btn("🔎 " + T.find_stories, async () => {
        b.disabled = true; b.textContent = "…";
        try {
          const d = await jpost("/reddit-discover", {});
          if (d.error) throw new Error(d.error);
          S.stories = d.stories || [];
          completeStep("discover", "pick");
        } catch (e) { b.disabled = false; b.textContent = "🔎 " + T.find_stories; errorCard(T.err_generic, String(e)); }
      }, "primary");
      c.appendChild(b);
    }
    setComposer("off"); return;
  }
  msgA(esc(T.pick_story));
  if (S.step === "pick" && !S.jobId) {
    const c = card();
    (S.stories || []).forEach((st, i) => {
      const b = el("button", "choice story-card",
        `<b>${esc(st.title || ("Story " + (i + 1)))}</b><p>${esc(String(st.body || st.text || "").slice(0, 180))}…</p>`);
      b.addEventListener("click", async () => {
        S.story = st;
        const d = await jpost("/reddit-generate", { story: st, story_id: st.id });
        if (d.error) { errorCard(T.err_generic, d.error); return; }
        const jid = (d.job || "").split("id=")[1] || d.job_id;
        if (jid) startJob(jid);
      });
      c.appendChild(b);
    });
  }
  setComposer("off");
}

/* ------------------------------------------------------------------ FLOW: longform video
   Paste the SCRIPT + pick the TTS + reasoning model - nothing else. The backend then runs
   voiceover -> exact-timestamp transcript -> one doodle prompt per timestamp -> FLUX.2 Pro
   16:9 images on Higgsfield (4 in flight) -> assembled MP4. */
function renderLongformFlow() {
  applySketchDefaults(S.longform);
  msgU(esc(T.mode_longform_t));
  msgA(esc(T.longform_script_q));
  const script = (S.longform.script || "").trim();
  if (S.completed.includes("script") && script) {
    msgU(esc(script.length > 220 ? script.slice(0, 220) + "…" : script), "script");
  } else if (!script || S.step === "script" || !S.completed.includes("script")) {
    const c = card();
    const existing = el("div", "lf-existing");
    const existingBtn = btn("Open an existing Sketch Explainer", async () => {
      existingBtn.disabled = true;
      try {
        const data = await jget("/projects-list");
        const projects = (data.projects || []).filter(project => project.longform);
        existing.innerHTML = "";
        if (!projects.length) {
          existing.appendChild(el("div", "card-note", "No existing Sketch Explainers yet."));
        } else {
          projects.forEach(project => {
            const row = el("button", "lf-project-choice");
            row.innerHTML = `<b>${esc(project.title || project.slug)}</b><span>${esc(project.status || "")}</span>`;
            row.addEventListener("click", () => openLongformProject(project));
            existing.appendChild(row);
          });
        }
      } catch (error) {
        existing.innerHTML = `<div class="card-note">${esc(String(error))}</div>`;
      } finally { existingBtn.disabled = false; }
    }, "ghost");
    existing.appendChild(existingBtn);
    c.appendChild(existing);
    const ta = el("textarea", "script-box");
    ta.placeholder = T.longform_script_ph; ta.rows = 12; ta.value = S.longform.script || "";
    ta.addEventListener("input", () => { S.longform.script = ta.value; persist(); });
    c.appendChild(ta);
    const foot = el("div", "card-foot");
    foot.appendChild(el("span", "spacer"));
    const go = btn(T.continue_btn || "Continue", () => {
      if ((S.longform.script || "").trim().length < 40) { ta.focus(); return; }
      completeStep("script", "settings");
    }, "primary");
    foot.appendChild(go);
    c.appendChild(foot);
    setComposer("off");
    return;
  }
  if (S.completed.includes("script") && !S.jobId && (S.step === "settings" || S.step === "review")) {
    const c = card();
    // Same anatomy as the script flow's Finish step: outSection blocks, not a flat stack of
    // fields - one section per decision, in the order you make them.
    // narrator: voice dropdown + Preview, same as the script flow (longform used to always use
    // the built-in default voice with no way to pick or hear one).
    let voiceFld = null;
    if ((OPT.tts_voice || []).length) {
      voiceFld = el("div", "fld voice-fld");
      voiceFld.appendChild(el("label", "", esc(T.tts_voice || "Narrator")));
      const voiceInline = el("div", "voice-inline");
      const vsel = el("select");
      resetTtsVoice(S.longform);
      ttsVoiceOptions(S.longform.tts_model).forEach(o => vsel.appendChild(new Option(o.label, o.value)));
      if (S.longform.tts_voice && [...vsel.options].some(o => o.value === S.longform.tts_voice)) vsel.value = S.longform.tts_voice;
      S.longform.tts_voice = vsel.value;
      vsel.addEventListener("change", () => { S.longform.tts_voice = vsel.value; persist(); });
      const prev = btn("▶ " + T.preview, () => {
        // Preview the performance the chosen format will actually use.  A Sketch Short has a
        // stronger opening read than a calm 16:9 explainer.
        previewTts(S.longform, (S.longform.aspect === "9:16") ? "sketch-short" : "longform");
      }, "ghost small");
      voiceInline.appendChild(vsel); voiceInline.appendChild(prev);
      voiceFld.appendChild(voiceInline);
    }
    // Same pipeline, two canvases: the long 16:9 explainer, or a ~1 minute 9:16 short with the
    // same drawing style and the same cut speed - only the script is much shorter.
    outSection(c, "Format", selectField("Video format", [
      { value: "16:9", label: "Long video - 16:9, full script" },
      { value: "9:16", label: "Short - 9:16 vertical, ~1 minute script" },
    ], S.longform.aspect || "16:9", v => {
      const changed = (S.longform.aspect || "16:9") !== v;
      S.longform.aspect = v;
      // A short lives or dies on energy, so it fills in the upbeat delivery instead of the
      // measured explainer read - but only into an EMPTY field, so anything typed by hand is
      // never overwritten by flipping the format back and forth.
      if (changed && v === "9:16" && !String(S.longform.tts_voice_instruction || "").trim()) {
        S.longform.tts_voice_instruction = SHORTS_VOICE_INSTRUCTION;
      }
      // A short is read faster than a twenty-minute explainer. Applied on the switch to 9:16
      // and undone on the way back, but only while the field still holds the other format's
      // default - a speed the user typed themselves is never touched.
      if (changed && v === "9:16" && String(S.longform.tts_native_speed || "1") === "1") {
        S.longform.tts_native_speed = SHORTS_VOICE_SPEED;
      } else if (changed && v === "16:9"
                 && String(S.longform.tts_native_speed || "") === SHORTS_VOICE_SPEED) {
        S.longform.tts_native_speed = "1";
      }
      persist(); if (changed) setTimeout(renderAll, 0);
    }), toggleField("Slow zoom on the images", S.longform.image_zoom === true,
      v => { S.longform.image_zoom = v; persist(); }),
    toggleField("Extra audio tracks in 7 languages", S.longform.multilang === true,
      v => { S.longform.multilang = v; persist(); renderAll(); }),
    el("div", "out-sec-hint",
      "A short keeps the same doodle style and cut rhythm; write about 150-170 words for one "
      + "minute. The zoom is a 5% push-in across each image's hold - enough to stop the frame "
      + "feeling frozen, not enough to read as camera movement."
      + (S.longform.multilang === true
        ? " Multi-language runs AFTER the video: the script is translated into Spanish, "
          + "Portuguese, Hindi, Indonesian, Japanese, German and French and spoken by a native "
          + "voice, one .mp3 per language in .renders for YouTube's audio-track upload. This is "
          + "seven translations and seven full voiceovers - on a 20-minute script it is the most "
          + "expensive thing here, and it is why the switch is off by default."
        : "")));
    // Google-search hook intro - a SHORTS feature, so the whole section only exists on the
    // 9:16 canvas. The hook is picked from the script's own sentences instead of a free-text
    // selection: at this step the script lives behind the previous card, so there is nothing
    // to select from, and the hook must be a sentence the narrator actually speaks anyway.
    if ((S.longform.aspect || "16:9") === "9:16") {
      const sentences = String(S.longform.script || "")
        .split(/(?<=[.!?])\s+/).map(t => t.trim()).filter(t => t.length > 11).slice(0, 8);
      if (S.longform.hook_text && !sentences.includes(S.longform.hook_text)) S.longform.hook_text = "";
      const hookBody = el("div", "hook-pick");
      const paintHookPick = () => {
        hookBody.innerHTML = "";
        if (S.longform.hook_intro !== true) return;
        sentences.forEach(t => {
          const b = btn((S.longform.hook_text === t ? "★ " : "") + t.slice(0, 76),
            () => { S.longform.hook_text = (S.longform.hook_text === t ? "" : t); persist(); paintHookPick(); },
            "ghost small hook-sentence" + (S.longform.hook_text === t ? " on" : ""));
          hookBody.appendChild(b);
        });
      };
      outSection(c, "Google hook intro",
        toggleField("Start with the typed Google search", S.longform.hook_intro === true,
          v => { S.longform.hook_intro = v; persist(); paintHookPick(); }),
        hookBody,
        el("div", "out-sec-hint",
          "A 4s clip of the hook being typed into Google, spoken by the same narrator, ending in "
          + "a fast zoom into the query. Mark the sentence to type - unmarked, the first sentence "
          + "is used."));
      paintHookPick();
    }
    const narrationSection = outSection(c, T.sec_narration || "Narration", voiceFld,
      OPT.longform_tts.length ? selectField(T.longform_tts, OPT.longform_tts, S.longform.tts_model,
        v => {
          const changed = S.longform.tts_model !== v;
          S.longform.tts_model = v; resetTtsVoice(S.longform); persist();
          if (changed) setTimeout(renderAll, 0);
        }) : null,
      el("div", "out-sec-hint", esc(T.longform_voice_hint)));
    const longformSeedSettings = seedTtsSettings(S.longform);
    if (longformSeedSettings) (narrationSection.querySelector(".out-sec-body") || narrationSection).appendChild(longformSeedSettings);
    if (OPT.longform_reasoning.length) {
      const dir = outSection(c, T.sec_director || "Director",
        selectField(T.longform_reasoning, OPT.longform_reasoning, S.longform.reasoning_model, v => { const changed=!!S.longform.reasoning_model&&S.longform.reasoning_model!==v; S.longform.reasoning_model = v; S.longform.reasoning_mode = reasoningOptions(v, S.longform.reasoning_mode).value; if(changed)setTimeout(renderAll,0); }));
      const body = dir.querySelector(".out-sec-body");
      appendReasoningField(body, S.longform.reasoning_model || firstVal(OPT.longform_reasoning), S.longform);
      // it hard-codes a 22px top margin for the bare card it normally lands on; inside a section
      // the body's own 14px gap sets the rhythm, and 22 on top of it breaks the grid
      const rm = body.lastElementChild;
      if (rm && rm.classList.contains("fld")) rm.style.marginTop = "";
    }
    // "Halt after speech": pause after TTS so every voiceover part can be approved/declined.
    // Same toggleField + hint the script flow's Speech section uses (this was a raw checkbox).
    outSection(c, T.sec_speech || "Speech",
      toggleField(T.longform_halt_speech, S.longform.halt_after_speech !== false,
        v => S.longform.halt_after_speech = v),
      el("div", "out-sec-hint", esc(T.longform_halt_hint)));
    outSection(c, T.connections,
      connectionRow("higgsfield", T.connect_higgsfield, "/higgsfield-status", "/higgsfield-login"));
    // Back is injected by decoratePrototypeFlow (it strips any manual one), so the footer only
    // carries the primary action - review-cta gives it the same weight as "Create Short".
    const foot = el("div", "card-foot");
    foot.appendChild(el("span", "spacer"));
    // a previously run project has frames on disk: let the user fix them without a new run
    if (S.projectSlug)
      foot.appendChild(btn("Generate 3 thumbnails + titles", generateLongformThumbnails, "secondary"));
    if (S.projectSlug)
      foot.appendChild(btn("View thumbnails + titles", () => openLongformThumbnailResults(S.projectSlug), "ghost"));
    if (S.projectSlug)
      foot.appendChild(btn("Open pre-render timeline", () => openLongformFrameEditor(S.projectSlug), "ghost"));
    if (S.projectSlug)
      foot.appendChild(btn("Review speech parts", reviewLongformSpeech, "ghost"));
    foot.appendChild(btn("🎬 " + T.create_longform, submitLongform, "primary review-cta"));
    c.appendChild(foot);
  }
  setComposer("off");
}
async function generateLongformThumbnails(ev) {
  if (!S.projectSlug) return;
  const trigger = ev && ev.currentTarget;
  if (trigger) { trigger.disabled = true; trigger.textContent = "Starting 3 thumbnails..."; }
  const data = await jpost("/longform-thumbnail", { slug: S.projectSlug });
  if (data && data.ok && data.id) {
    startJob(data.id);
    return;
  }
  if (trigger) { trigger.disabled = false; trigger.textContent = "Generate 3 thumbnails + titles"; }
  errorCard(T.err_generic, (data && data.error) || "Could not start thumbnail generation.");
}
async function reviewLongformSpeech() {
  if (!S.projectSlug) return;
  const data = await jpost("/longform-speech-review", { slug: S.projectSlug });
  if (!data || !data.ok) {
    errorCard(T.err_generic, (data && data.error) || "Could not open speech parts.");
    return;
  }
  const jid = data.id || String(data.job || "").split("id=")[1];
  if (jid) startJob(jid);
}
async function submitLongform() {
  const fd = new FormData();
  fd.append("script", S.longform.script || "");
  fd.append("tts_model", S.longform.tts_model || firstVal(OPT.longform_tts) || "pro");
  fd.append("tts_voice", S.longform.tts_voice || firstVal(OPT.tts_voice) || "");
  ["tts_voice_instruction", "tts_language", "tts_native_speed", "tts_volume", "tts_pitch", "tts_sample_rate", "tts_output_format"].forEach(k => {
    if (S.longform[k] != null) fd.append(k, S.longform[k]);
  });
  fd.append("aspect", S.longform.aspect || "16:9");
  if (S.longform.image_zoom === true) fd.append("image_zoom", "on");
  if (S.longform.multilang === true) fd.append("multilang", "on");
  if (S.longform.aspect === "9:16" && S.longform.hook_intro === true) {
    fd.append("hook_intro", "on");
    if (S.longform.hook_text) fd.append("hook_text", S.longform.hook_text);
  }
  fd.append("reasoning_model", S.longform.reasoning_model || firstVal(OPT.longform_reasoning) || "google/gemini-3.7-flash");
  fd.append("reasoning_mode", S.longform.reasoning_mode || "");
  if (S.longform.halt_after_speech !== false) fd.append("halt_after_speech", "on");
  const c = card(); c.appendChild(el("div", "dots", "<i></i><i></i><i></i>"));
  try {
    const r = await fetch("/longform-run", { method: "POST", body: fd });
    const jid = new URL(r.url, location.href).searchParams.get("id");
    if (!jid) throw new Error("no job id");
    startJob(jid);
  } catch (e) { c.remove(); errorCard(T.err_generic, String(e)); }
}

/* ------------------------------------------------------------------ FLOW: loaded project */
function renderProjectFlow() {
  msgA(`<b>${esc(T.project_loaded)}</b>: ${esc(S.projectTitle || S.projectSlug)}`);
  const c = card();
  const g = el("div", "sum-grid");
  const info = S._projInfo || {};
  [["Title", S.projectTitle || S.projectSlug], ["Status", info.failed ? "Failed / incomplete" : "OK"],
   ["Last edited", info.edited || ""], ["Script", (S.values.script || "").slice(0, 120) || "-"],
   ["Visual source", S.values.clip_source === "scrape" ? T.src_scrape : T.src_generate]]
    .forEach(([k, v]) => { g.appendChild(el("span", "k", esc(k))); g.appendChild(el("span", "v", esc(v))); g.appendChild(el("span")); });
  c.appendChild(g);
  const acts = el("div", "card-foot");
  const runModes = [
    ["normal", T.run_normal], ["recut_existing_only", T.run_recut],
    ["recut_new_web_images", T.run_new_web], ["recut_regenerate_seedance", T.run_regen_clips],
    ["recut_recreate_speaker_clip", T.run_recreate_hook],
  ];
  runModes.forEach(([mode, label]) => acts.appendChild(btn(label, () => {
    S.values.loaded_project_mode = mode;
    S.values.loaded_project_source = S.projectSlug;
    S.flow = "script";
    S.completed = ["script", "voice", "source", "reasoning", "outputs"];
    S.step = "review";
    renderAll(); persist();
  }, "small")));
  c.appendChild(acts);
  const acts2 = el("div", "card-foot");
  if (S._projInfo && S._projInfo.failed)
    acts2.appendChild(btn("▶ " + T.continue_project, () => continueProject(S.projectSlug), "primary small"));
  if (S._projInfo && S._projInfo.has_timeline)
    acts2.appendChild(linkBtn("🎞 " + T.open_timeline, "/timeline?slug=" + encodeURIComponent(S.projectSlug), "small"));
  if (S._projInfo && S._projInfo.results_url)
    acts2.appendChild(linkBtn("▶ " + T.check_results, S._projInfo.results_url, "small"));
  acts2.appendChild(btn(T.rename, () => renameProject(S.projectSlug, S.projectTitle), "ghost small"));
  c.appendChild(acts2);
  setComposer("off");
}
async function loadProject(slug, knownInfo) {
  try {
    showLoading(T.project_loaded + "...");
    // Detach the previous completed job so its "Your short is ready" card cannot leak
    // into the newly selected project's deterministic render.
    S.jobId = null; S.jobStatus = "";
    announcedPhases = []; lastProgressHTML = lastMediaHTML = lastOutputsHTML = "";
    lastAssignedKey = "";
    history.replaceState(null, "", "/?project=" + encodeURIComponent(slug));
    // the list entry is fetched anyway (for S._projInfo); read it FIRST so a longform project
    // never reaches /project-preset, which only knows clip projects and would answer with defaults
    // The caller usually just rendered this entry, so re-fetching the whole list costs a full
    // scan of every project on disk for data already in hand - measured at 4.9s for 204 projects
    // before the scan itself was fixed, and it ran TWICE for one click.
    let info = knownInfo;
    if (!info || info.slug !== slug) {
      const plist = await jget("/projects-list");
      info = (plist.projects || []).find(p => p.slug === slug) || {};
    }
    if (info.longform) { openLongformProject(info); return; }
    // A dreamcore project that is still waiting for its clips has no render and no
    // timeline to open - it reopens on its own prompt list, which is the whole reason it
    // is saved at all.
    if (info.dreamcore && info.awaiting) { await openDreamcoreProject(slug); return; }
    const d = await jget("/project-preset?slug=" + encodeURIComponent(slug));
    S.projectSlug = d.slug; S.projectTitle = d.title || d.slug;
    const st = d.state || {};
    Object.keys(st).forEach(k => {
      if (MAN.run.text.includes(k)) S.values[k] = st[k] != null ? String(st[k]) : S.values[k];
      if (MAN.run.check.includes(k)) S.values[k] = !!st[k];
      if (MAN.run.state_hidden.includes(k)) S.values[k] = !!st[k];
    });
    S.values.loaded_project_source = d.slug;
    S._projInfo = info;                 // already fetched above
    S.flow = "project"; S.step = "summary"; S.completed = [];
    S.view = "chat";
    hideLoading();
    renderAll(); persist();
  } catch (e) { hideLoading(); errorCard(T.err_generic, String(e)); }
}
async function openDreamcoreProject(slug) {
  const d = await jget("/dreamcore-project?slug=" + encodeURIComponent(slug));
  const D = dreamcoreState();
  D.slug = d.slug || slug;
  D.brief = d.brief || "";
  D.world = d.world || "";
  D.prompts = d.prompts || [];
  D.grid = d.grid || null;
  D.bed = (d.bed || "").split(/[\/]/).pop();
  DREAMCORE_FILES = new Array(D.prompts.length).fill(null);
  S.projectSlug = D.slug;
  S.projectTitle = d.title || ("Dreamcore: " + (D.brief || slug));
  S.flow = "dreamcore"; S.step = "clips"; S.completed = ["brief"]; S.draft = true;
  S.view = "chat"; S.jobId = null; S.jobStatus = "";
  hideLoading();
  renderAll(); persist();
}

// Longform has no "project overview" of its own: it resumes by re-running the same script, and the
// pipeline reuses the voiceover it already paid for. So opening one lands you in its Production
// step with the script and narrator restored - Create picks up exactly where the run stopped.
function openLongformProject(info) {
  S.projectSlug = info.slug;
  S.projectTitle = info.title || info.slug;
  S.flow = "longform"; S.step = "settings"; S.completed = ["script"]; S.draft = true;
  S.longform.script = info.script || "";
  ["tts_model", "tts_voice", "reasoning_model", "reasoning_mode",
   "tts_voice_instruction", "tts_language", "tts_native_speed", "tts_volume",
   "tts_pitch", "tts_sample_rate", "tts_output_format"].forEach(k => {
    if (info[k] != null && info[k] !== "") S.longform[k] = info[k];
  });
  ["mascot_enabled", "halt_after_speech"].forEach(k => {
    if (info[k] != null) S.longform[k] = !!info[k];
  });
  S.view = "chat";
  hideLoading();
  renderAll(); persist();
  // Once narration exists, reopening is a resume action: go straight to the
  // pre-render timeline instead of asking for narration settings again.
  if (info.voiceover_ready) {
    setTimeout(() => openLongformPreRenderEditor(info.slug), 0);
  }
}
async function continueProject(slug) {
  const d = await jpost("/resume-project", { slug });
  if (d && d.ok && d.job) {
    const jid = (d.job || "").split("id=")[1];
    if (jid) { startJob(jid); return; }
  }
  errorCard(T.err_generic, (d && d.error) || "Could not continue this project.");
}

/* ------------------------------------------------------------------ job experience */
function startJob(jobId, jobKind) {
  // Active jobs can be opened while the asset library is selected.  A job is a processing view,
  // so always leave that library state first; otherwise renderAll() immediately returns from its
  // assets branch and the click appears to have opened Projects & Assets instead of the job.
  S.view = "chat";
  S.jobId = jobId; S.jobStatus = "running"; S.draft = false;
  if (jobKind) {
    S.jobKind = String(jobKind);
    const flowForKind = { longform:"longform", sfx:"sfx", visual:"visual", asmr:"asmr",
      caption:"captions", reddit:"reddit", physics:"physics", lowpoly:"lowpoly", aicore:"aicore" };
    S.flow = flowForKind[S.jobKind] || "script";
  }
  announcedPhases = []; lastProgressHTML = lastMediaHTML = lastOutputsHTML = "";
  lastAssignedKey = ""; lastLiveTimelineKey = "";
  history.replaceState(null, "", "/job?id=" + encodeURIComponent(jobId));
  renderAll(); persist();
}
/* deterministic typewriter for assistant bubbles (visual only; text is template-fixed) */
const REDUCED = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
function typeInto(node, text) {
  if (REDUCED || text.length > 160) { node.textContent = text; return; }
  node.textContent = ""; node.classList.add("typing");
  let i = 0;
  const tick = () => {
    node.textContent = text.slice(0, ++i);
    if (i < text.length) setTimeout(tick, 14);
    else node.classList.remove("typing");
  };
  tick();
}
function typedMsg(container, text) {
  const m = el("div", "msg assistant");
  m.appendChild(el("div", "who", "Shortslab"));
  const b = el("div", "bubble"); m.appendChild(b);
  (container || chat).appendChild(m);
  typeInto(b, text);
  scrollDown();
  return m;
}

// Recolor the run box by state: running=green, awaiting-approval(paused)=orange, error/cancel=red.
function setJobBoxState(cardEl, status) {
  if (!cardEl) return;
  const st = String(status || "").toLowerCase();
  let cls = "prog-state-running";
  if (st === "awaiting_approval") cls = "prog-state-paused";
  else if (["error", "failed", "cancelled", "canceled", "cancelling"].includes(st)) cls = "prog-state-error";
  else if (["done", "complete", "completed"].includes(st)) cls = "prog-state-done";
  cardEl.classList.remove("prog-state-running", "prog-state-paused", "prog-state-error", "prog-state-done");
  cardEl.classList.add(cls);
}
function renderJobSection() {
  if (prototypeMode) {
    const hero = el("header", "production-head");
    hero.innerHTML = `<span>PRODUCTION RUN</span><h1>Your project is in production.</h1><p>Follow live progress and assigned footage while Shortslab builds the video.</p>`;
    chat.appendChild(hero);
  } else typedMsg(chat, T.project_started);
  // V3 publishes its edit map as soon as the visual beats are decided. It remains hidden before
  // that point, then fills in-place as inspected footage is assigned to each beat.
  const liveTimeline = el("section", "live-process-timeline");
  liveTimeline.id = "live-process-timeline"; liveTimeline.hidden = true;
  liveTimeline.setAttribute("aria-label", "Live edit timeline");
  liveTimeline.setAttribute("aria-live", "polite");
  liveTimeline.innerHTML =
    '<button class="lpt-head" id="lpt-toggle" type="button" aria-expanded="true">' +
      '<span class="lpt-heading"><small>LIVE EDIT MAP</small><b>Building the timeline</b></span>' +
      '<span class="lpt-summary" id="lpt-summary"></span>' +
      '<svg class="lpt-chevron" viewBox="0 0 24 24" aria-hidden="true"><path d="m7 10 5 5 5-5"/></svg>' +
    '</button><div class="lpt-collapse"><div class="lpt-body">' +
      '<div class="lpt-status"><span id="lpt-status-text">Visual beats ready</span>' +
      '<span id="lpt-duration"></span></div>' +
      '<div class="lpt-scroll"><div class="lpt-ruler" id="lpt-ruler"></div>' +
      '<div class="lpt-track" id="lpt-track"></div></div>' +
      '<div class="lpt-legend"><span><i class="planned"></i>Planned beat</span>' +
      '<span><i class="assigned"></i>Footage assigned</span>' +
      '<span><i class="exact"></i>Exact match</span>' +
      '<span><i class="context"></i>Contextual</span></div>' +
    '</div></div>';
  chat.appendChild(liveTimeline);
  const lptToggle = liveTimeline.querySelector("#lpt-toggle");
  let lptOpen = localStorage.getItem("shortslab-live-timeline-open") !== "0";
  const syncLptOpen = () => {
    liveTimeline.classList.toggle("collapsed", !lptOpen);
    lptToggle.setAttribute("aria-expanded", lptOpen ? "true" : "false");
  };
  syncLptOpen();
  lptToggle.addEventListener("click", () => {
    lptOpen = !lptOpen; localStorage.setItem("shortslab-live-timeline-open", lptOpen ? "1" : "0");
    syncLptOpen();
  });
  // #111 - assigned footage grid (ONLY the clips chosen for scenes) sits at the TOP
  const mwrap = el("div"); mwrap.id = "job-media"; chat.appendChild(mwrap);
  const scrapeMonitor=el("section","scrape-monitor"); scrapeMonitor.id="scrape-monitor"; scrapeMonitor.hidden=true;
  const scrapeView=el("div","scrape-browser-card"); scrapeView.id="scrape-browser-card"; scrapeView.hidden=true;
  scrapeView.innerHTML='<div class="scrape-browser-head"><b>Live scrape browser</b><span id="scrape-browser-meta"></span></div><img id="scrape-browser-image" alt="Current TikTok or X scraper page">';
  const acceptedView=el("div","last-accepted-card"); acceptedView.id="last-accepted-card"; acceptedView.hidden=true;
  acceptedView.innerHTML='<div class="last-accepted-head"><b>Last accepted:</b><span id="last-accepted-meta"></span></div><div class="last-accepted-media"><img id="last-accepted-poster" alt="Last accepted clip frame" hidden><video id="last-accepted-video" muted loop playsinline preload="metadata"></video><span id="last-accepted-empty">Waiting for a matching clip...</span></div><div class="last-accepted-query" id="last-accepted-query"></div>';
  scrapeMonitor.appendChild(scrapeView); scrapeMonitor.appendChild(acceptedView); chat.appendChild(scrapeMonitor);
  // technical console -> opens as an OVERLAY POPUP (not inline). Its trigger button now lives in
  // the run-box header, right next to Cancel (where the status badge used to be).
  const overlay = el("div", "tech-modal-overlay"); overlay.id = "job-tech-overlay"; overlay.hidden = true;
  const modal = el("div", "tech-modal");
  const mhead = el("div", "tech-modal-head");
  mhead.innerHTML = `<span class="term-dot"></span><span class="term-dot"></span><span class="term-dot"></span><b>${esc(T.show_tech)}</b>`;
  const closeB = el("button", "tech-modal-close"); closeB.innerHTML = "&times;"; closeB.setAttribute("aria-label", "Close");
  mhead.appendChild(closeB); modal.appendChild(mhead);
  const lg = el("pre", "tech-log"); lg.id = "job-log";
  modal.appendChild(lg); overlay.appendChild(modal);
  (document.querySelector(".app") || document.body).appendChild(overlay);
  const openTech = () => { overlay.hidden = false; lg.scrollTop = lg.scrollHeight; };
  const closeTech = () => { overlay.hidden = true; };
  closeB.addEventListener("click", closeTech);
  overlay.addEventListener("click", (e) => { if (e.target === overlay) closeTech(); });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !overlay.hidden) closeTech(); });
  // #111 - deterministic phase stats stack in this container, which sits directly ABOVE the run
  // box. Each new stat is appended to the bottom (right above the box), so the newest is always
  // closest to the box and older stats move up.
  const events = el("div", "job-events-stack"); events.id = "job-events"; chat.appendChild(events);
  // The assigned card lives INSIDE the monitor row, not above it: the live browser is what you
  // watch, and a band stacked on top of it just pushes it down. Appended after the accepted card
  // so the row reads left-to-right: what the scraper sees -> what it took -> what it placed.
  scrapeMonitor.appendChild(mwrap);
  // speech approval placeholder (interactive gate, right above the run box)
  const speech = el("div"); speech.id = "job-speech"; chat.appendChild(speech);
  // ---- hero progress card: FURTHEST DOWN, big prominent bar, pulsing status + elapsed
  const c = card("prog-card"); c.id = "job-card";
  const head = el("div", "prog-head");
  const dot = el("span", "status-pulse"); dot.id = "job-dot";
  head.appendChild(dot);
  const runTitle = el("b", "", S.flow === "longform"
    ? "Creating your longform video" : "Creating your Short");
  runTitle.id = "job-kind-title";
  head.appendChild(runTitle);
  head.appendChild(el("span", "spacer"));
  // "Show technical details" button next to Cancel (replaces the status badge text)
  const techBtn = el("button", "job-tech-btn"); techBtn.id = "job-tech-btn"; techBtn.type = "button";
  techBtn.innerHTML = `<span class="term-dot"></span><span class="term-dot"></span><span class="term-dot"></span><span class="term-title">${esc(T.show_tech)}</span>`;
  techBtn.addEventListener("click", openTech);
  head.appendChild(techBtn);
  const cancelB = btn(T.cancel_process, cancelJob, "danger small"); cancelB.id = "job-cancel";
  head.appendChild(cancelB);
  c.appendChild(head);
  setJobBoxState(c, S.jobStatus || "running");
  const prog = el("div", "prog-embed"); prog.id = "job-progress"; c.appendChild(prog);
  // outputs / result container (appears below the box when the run completes)
  const owrap = el("div"); owrap.id = "job-out"; chat.appendChild(owrap);
  pollJob(); pollTimer = setInterval(pollJob, 2500);
  setComposer("off");
}
// LIVE SCREENING PANEL: during a discovery run more than half the viewport sat empty
// while the user waited minutes on a single log line (UI review 2026-07-25). The run's
// own log already carries every verdict, so parse it and show the material as it is
// judged - accepted picks first, rejects with their reason underneath.
const SCREEN_RE = {
  trying: /Discovery: trying @([^\s(]+)\s*\((\d+)s,\s*([\d,]+) likes\)\s*-\s*(.*)$/,
  accept: /Discovery: candidate (\d+)\/(\d+) accepted - (.+?)(?:\s*\[([\d.]+) cuts\/min\])?$/,
  skip:   /Discovery: skipped @([^\s]+) - (.+)$/,
  reject: /Discovery: rejected by vision review/,
  vision: /(?:Mini d|D)iscovery vision: appeal (\d+)\/10, (\d+) story beats - (.+)$/,
};
function parseScreening(logText) {
  const lines = String(logText || "").split("\n");
  const items = []; let cur = null;
  for (const raw of lines) {
    const line = raw.trim(); let m;
    if ((m = line.match(SCREEN_RE.trying))) {
      cur = { author: m[1], dur: +m[2], likes: m[3], desc: m[4], state: "checking" };
      items.push(cur); continue;
    }
    if ((m = line.match(SCREEN_RE.vision)) && cur) { cur.appeal = +m[1]; cur.title = m[3]; continue; }
    if ((m = line.match(SCREEN_RE.accept))) {
      if (cur) { cur.state = "accepted"; cur.title = m[3]; if (m[4]) cur.cuts = m[4]; }
      continue;
    }
    if ((m = line.match(SCREEN_RE.skip))) {
      if (cur) { cur.state = "rejected"; cur.reason = m[2].replace(/\.$/, ""); }
      continue;
    }
    if (SCREEN_RE.reject.test(line) && cur) { cur.state = "rejected"; cur.reason = "vision review"; continue; }
  }
  return items;
}
function renderScreeningPanel(d) {
  const host = $("job-progress"); if (!host) return;
  const items = parseScreening(d && d.log_text);
  let panel = $("job-screening");
  if (!items.length) { if (panel) panel.remove(); return; }
  if (!panel) {
    panel = el("section", "scr-panel"); panel.id = "job-screening";
    panel.setAttribute("aria-live", "polite");
    panel.innerHTML = '<div class="scr-head"><h4>Material screening</h4><span class="scr-count"></span></div><div class="scr-list"></div>';
    host.parentNode.insertBefore(panel, host.nextSibling);
  }
  const accepted = items.filter(i => i.state === "accepted");
  panel.querySelector(".scr-count").textContent =
    `${accepted.length} kept / ${items.length} checked`;
  const list = panel.querySelector(".scr-list");
  // only append what is new -> no flicker, no re-layout of what the user is reading
  const have = list.children.length;
  items.slice(have).forEach((it, k) => {
    const row = el("article", "scr-row scr-" + it.state);
    row.style.animationDelay = Math.min(k, 6) * 40 + "ms";   // staggered entrance
    row.innerHTML =
      '<span class="scr-dot" aria-hidden="true"></span>' +
      '<div class="scr-body"><b>' + esc(it.title || it.desc || ("@" + it.author)) + "</b>" +
      '<span>@' + esc(it.author) + " &middot; " + it.dur + "s &middot; " + esc(it.likes) + " likes" +
      (it.appeal ? " &middot; appeal " + it.appeal + "/10" : "") +
      (it.cuts ? " &middot; " + it.cuts + " cuts/min" : "") + "</span></div>" +
      '<span class="scr-verdict">' +
      (it.state === "accepted" ? "KEPT" : it.state === "rejected"
        ? esc(String(it.reason || "rejected").slice(0, 34)) : "checking\u2026") + "</span>";
    list.appendChild(row);
  });
  for (let i = 0; i < list.children.length && i < items.length; i++) {
    const want = "scr-row scr-" + items[i].state;
    if (list.children[i].className !== want) {
      list.children[i].className = want;
      const v = list.children[i].querySelector(".scr-verdict");
      if (v) v.textContent = items[i].state === "accepted" ? "KEPT"
        : items[i].state === "rejected" ? String(items[i].reason || "rejected").slice(0, 34)
        : "checking\u2026";
    }
  }
  list.scrollTop = list.scrollHeight;
}

function stopPolling() { if (pollTimer) { clearInterval(pollTimer); pollTimer = null; } if(scrapePreviewTimer){clearInterval(scrapePreviewTimer);scrapePreviewTimer=null;} }

function setScrapePreviewPolling(enabled) {
  if (!enabled) {
    if (scrapePreviewTimer) { clearInterval(scrapePreviewTimer); scrapePreviewTimer = null; }
    const monitor=$("scrape-monitor"); if (monitor) monitor.hidden=true;
    return;
  }
  if (scrapePreviewTimer) return;
  pollScrapeBrowser();
  scrapePreviewTimer=setInterval(pollScrapeBrowser,3000);
}

async function pollScrapeBrowser(){
  const monitor=$("scrape-monitor"),card=$("scrape-browser-card"),image=$("scrape-browser-image"),meta=$("scrape-browser-meta");
  const accepted=$("last-accepted-card"),video=$("last-accepted-video"),acceptedMeta=$("last-accepted-meta"),acceptedQuery=$("last-accepted-query"),empty=$("last-accepted-empty"),poster=$("last-accepted-poster");
  if(!card||!image)return;
  try{
    const d=await jget('/scrape-browser-status?job_id='+encodeURIComponent(S.jobId||''));
    card.hidden=!d.available;
    if(monitor)monitor.hidden=!d.available&&!d.last_accepted_url;
    meta.textContent=[d.platform,d.query,d.sort].filter(Boolean).join(' · ');
    if(d.available&&String(image.dataset.version||'')!==String(d.version)){
      image.dataset.version=String(d.version); image.src='/scrape-browser-preview?job_id='+encodeURIComponent(S.jobId||'')+'&v='+encodeURIComponent(d.version);
    }
    if(accepted){
      // Only reveal the "Last accepted" card ONCE a clip has actually been accepted - it used to
      // show (empty + stretched into the wide browser column) before scraping produced anything.
      const has=!!(d.last_accepted_poster_url||d.last_accepted_url);
      accepted.hidden=!has;
      acceptedMeta.textContent=d.last_accepted_platform||'';
      acceptedQuery.textContent=d.last_accepted_query?('Search: '+d.last_accepted_query):'';
      // POSTER FRAME is the reliable display (proxies are often HEVC and won't play inline);
      // the <video> is a best-effort enhancement layered on top when the codec is supported.
      if(empty)empty.hidden=has;
      if(poster&&d.last_accepted_poster_url&&String(poster.dataset.version||'')!==String(d.accepted_version)){
        poster.dataset.version=String(d.accepted_version); poster.src=d.last_accepted_poster_url; poster.hidden=false;
      }
      if(d.last_accepted_url&&String(video.dataset.version||'')!==String(d.accepted_version)){
        video.dataset.version=String(d.accepted_version); video.dataset.start=String(d.last_accepted_start||0);
        if(d.last_accepted_poster_url)video.setAttribute('poster',d.last_accepted_poster_url);
        video.src=d.last_accepted_url; video.hidden=false;
        video.onloadedmetadata=function(){try{video.currentTime=Math.min(Math.max(0,+video.dataset.start||0),Math.max(0,(video.duration||0)-.1));video.play().catch(function(){});}catch(e){}};
      }
    }
  }catch(e){card.hidden=true;if(monitor)monitor.hidden=true;}
}

/* deterministic event → message adapter (template text only; typed like a chatbot) */
const PHASE_RULES = [
  [/speech (generation )?(complete|ready)|voiceover ready/i, "Speech generation completed."],
  [/scraping|searching tiktok|social search|scrape v2: searching/i, "Searching TikTok for relevant footage."],
  [/rendering (the )?final|final render|rendering frames/i, "Rendering the final video."],
  [/mixing audio|sound design|placing editor sfx/i, "Adding the sound design."],
];
function announcePhases(logText) {
  const lines = (logText || "").split("\n").slice(-40);
  lines.forEach(line => {
    PHASE_RULES.forEach(([re, msg]) => {
      if (!re.test(line)) return;
      const text = msg;
      if (!text || announcedPhases.includes(text)) return;
      announcedPhases.push(text);
      const evts = $("job-events");
      if (evts) {
        if (prototypeMode) evts.appendChild(el("div", "job-event", `<i></i><span>${esc(text)}</span>`));
        else typedMsg(evts, text);
      }
    });
  });
}

/* assigned footage grid: only the run's ACTUAL scene choices, clean fixed tiles */
let lastAssignedKey = "";
let lastLiveTimelineKey = "";
function renderLiveProcessingTimeline(data) {
  const host = $("live-process-timeline");
  if (!host) return;
  if (!data || !(data.beats || []).length) {
    host.hidden = true; lastLiveTimelineKey = ""; return;
  }
  host.hidden = false;
  const beats = data.beats || [];
  const duration = Math.max(.1, +data.duration || Math.max(...beats.map(b => +b.end || 0)));
  const assignedCount = beats.reduce((n, b) => n + ((b.assigned || []).length ? 1 : 0), 0);
  const clipCount = beats.reduce((n, b) => n + (b.assigned || []).length, 0);
  const summary = $("lpt-summary");
  if (summary) summary.textContent = `${assignedCount} / ${beats.length} beats filled`;
  const statusText = $("lpt-status-text");
  if (statusText) statusText.textContent = data.phase === "ready"
    ? "Edit map ready" : clipCount ? "Footage is landing on the edit" : "Visual beats ready — searching footage";
  const durationEl = $("lpt-duration");
  if (durationEl) durationEl.textContent = `${clock(duration)} total`;
  const key = JSON.stringify(beats.map(b => [b.id, b.start, b.end, (b.assigned || []).map(a => a.url)]));
  if (key === lastLiveTimelineKey) return;
  lastLiveTimelineKey = key;

  const ruler = $("lpt-ruler"), track = $("lpt-track");
  if (!ruler || !track) return;
  if (ruler.dataset.duration !== String(duration)) {
    ruler.dataset.duration = String(duration); ruler.innerHTML = "";
    const tickStep = duration <= 25 ? 5 : duration <= 60 ? 10 : 15;
    for (let t = 0; t <= duration + .01; t += tickStep) {
      const tick = el("span", "lpt-tick", clock(t));
      tick.style.left = `${Math.min(100, t / duration * 100)}%`; ruler.appendChild(tick);
    }
  }
  const wantedIds = new Set(beats.map((beat, index) => String(beat.id == null ? index : beat.id)));
  track.querySelectorAll(".lpt-beat").forEach(tile => {
    if (!wantedIds.has(tile.dataset.beatId)) tile.remove();
  });
  beats.forEach((beat, index) => {
    const media = beat.assigned || [];
    const beatId = String(beat.id == null ? index : beat.id);
    let tile = track.querySelector(`.lpt-beat[data-beat-id="${CSS.escape(beatId)}"]`);
    const isNew = !tile;
    if (!tile) {
      tile = el("article", "lpt-beat"); tile.dataset.beatId = beatId;
      tile.appendChild(el("div", "lpt-visual"));
      tile.appendChild(el("div", "lpt-beat-copy"));
      tile.style.animationDelay = Math.min(index, 7) * 32 + "ms";
      track.appendChild(tile);
    }
    tile.classList.toggle("has-media", !!media.length);
    tile.classList.toggle("is-waiting", !media.length);
    tile.style.flexGrow = String(Math.max(.35, (+beat.end || 0) - (+beat.start || 0)));
    tile.title = beat.voice || beat.title || "Visual beat";
    const mediaKey = media.map(item => item.url).join("|");
    if (isNew || tile.dataset.mediaKey !== mediaKey) {
      tile.dataset.mediaKey = mediaKey;
      const visual = tile.querySelector(".lpt-visual"); visual.innerHTML = "";
      if (media.length) {
        // Each clip gets its OWN cell, widened by how much of the beat it actually covers.
        // Three thumbnails sharing one strip gave equal widths and no identity, so a beat
        // carried by one long shot looked the same as one stitched from three scraps.
        const shown = media.slice(0, 4);
        const spans = shown.map(item =>
          Math.max(.15, (+item.source_end || 0) - (+item.source_start || 0)));
        const spanTotal = spans.reduce((a, b) => a + b, 0) || shown.length;
        shown.forEach((item, order) => {
          const cell = el("div", "lpt-clip");
          const cls = String(item.match_class || "context").toLowerCase();
          cell.classList.add("match-" + (["exact", "context", "proxy"].includes(cls) ? cls : "context"));
          cell.style.flexGrow = String(spans[order] / spanTotal * shown.length);
          if (item.type === "video") {
            const video = document.createElement("video");
            video.src = item.url; video.muted = true; video.playsInline = true;
            video.preload = "metadata"; cell.appendChild(video);
            video.addEventListener("loadedmetadata", () => {
              try { video.currentTime = Math.min(+item.source_start || .05, Math.max(.05, video.duration - .08)); } catch (_e) {}
            }, { once:true });
          } else {
            const image = document.createElement("img"); image.src = item.url;
            image.alt = "Assigned footage"; image.loading = "lazy"; cell.appendChild(image);
          }
          const tag = el("div", "lpt-clip-tag");
          tag.appendChild(el("i", ""));
          tag.appendChild(document.createTextNode(
            (item.platform || "clip").slice(0, 9) + (cls === "exact" ? " · exact" : "")));
          cell.appendChild(tag);
          // The window this clip was cut from - the single most useful thing to see while a
          // run is still choosing shots, and it was in the payload all along.
          const span = spans[order];
          if (span > .2) {
            cell.appendChild(el("div", "lpt-clip-win",
              `${clock(+item.source_start || 0)}+${span.toFixed(1)}s`));
          }
          cell.title = `${item.name || "clip"}
${item.platform || ""} · ${cls}` +
            `
source ${clock(+item.source_start || 0)} – ${clock(+item.source_end || 0)}`;
          visual.appendChild(cell);
        });
        if (media.length > shown.length) {
          visual.appendChild(el("div", "lpt-clip-more", "+" + (media.length - shown.length)));
        }
      } else visual.appendChild(el("span", "lpt-search-pulse", ""));
    }
    const exactCount = media.filter(m => String(m.match_class || "").toLowerCase() === "exact").length;
    const beatSeconds = Math.max(0, (+beat.end || 0) - (+beat.start || 0));
    tile.querySelector(".lpt-beat-copy").innerHTML =
      `<small>${clock(+beat.start || 0)} – ${clock(+beat.end || 0)} · ${beatSeconds.toFixed(1)}s</small>` +
      `<b>${esc(beat.title || `Beat ${index + 1}`)}</b>` +
      `<em>${media.length
        ? `${media.length} clip${media.length === 1 ? "" : "s"}` +
          (exactCount ? ` · ${exactCount} exact` : " · contextual")
        : "Searching…"}</em>`;
  });
}
function renderAssignedMedia(items) {
  const mw = $("job-media");
  if (!mw) return;
  const key = (items || []).map(i => i.url).join("|");
  if (key === lastAssignedKey) return;
  lastAssignedKey = key;
  mw.innerHTML = "";
  if (!items || !items.length) return;
  // Only the NEWEST assignment. The full grid was a wide band ABOVE the live browser, which is the
  // thing you actually want to watch; one 9:16 tile beside it says the same thing in the shape the
  // clip really has. The count stays in the header so nothing is hidden.
  // Trade-off: the per-clip exclude (am-x) now only reaches the newest clip - the rest are
  // excludable from the media library after the run.
  const total = items.length;
  const shown = items.slice(-1);
  if (prototypeMode) mw.appendChild(el("div", "assigned-head", `<span>ASSIGNED FOOTAGE</span><b>${total} clip${total === 1 ? "" : "s"} chosen for your scenes</b>`));
  else typedMsg(mw, `Assigned footage — ${total} clip${total === 1 ? "" : "s"} chosen for your scenes.`);
  const c = el("div", "chat-card am-card");
  const grid = el("div", "am-grid");
  shown.forEach(it => {
    const t = el("div", "am-tile");
    if (it.type === "video") {
      const v = el("video"); v.muted = true; v.preload = "none"; v.setAttribute("playsinline", "");
      v.dataset.src = it.url;
      // show a real first frame once loaded (not a black box)
      v.addEventListener("loadeddata", () => { try { if (v.currentTime < 0.03) v.currentTime = 0.08; } catch (e) {} }, { once: true });
      t.appendChild(v);
      t.addEventListener("mouseenter", () => { if (!v.src) { v.preload = "auto"; v.src = v.dataset.src; } v.play().catch(() => {}); });
      t.addEventListener("mouseleave", () => { try { v.pause(); } catch (e) {} });
    } else {
      const im = el("img"); im.loading = "lazy"; im.src = it.url; t.appendChild(im);
    }
    const x = el("button", "am-x", "✕");
    x.title = "Exclude this clip from the run"; x.setAttribute("aria-label", "Exclude clip");
    x.addEventListener("click", async (ev) => {
      ev.stopPropagation(); x.disabled = true;
      await fetch("/exclude-run-media", { method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        body: new URLSearchParams({ id: S.jobId, path: it.path }) });
      t.classList.add("gone"); setTimeout(() => t.remove(), 250);
    });
    t.appendChild(x);
    t.appendChild(el("span", "am-name", esc(String(it.name || "").replace(/^scraped_/, "").slice(0, 22))));
    grid.appendChild(t);
  });
  c.appendChild(grid);
  mw.appendChild(c);
  loadVisibleAssigned(grid);
}
function loadVisibleAssigned(grid) {
  // load first-frame posters lazily: give each on-screen tile a src (metadata only)
  if (!("IntersectionObserver" in window)) {
    grid.querySelectorAll("video[data-src]").forEach(v => { v.preload = "metadata"; v.src = v.dataset.src; });
    return;
  }
  const io = new IntersectionObserver(es => {
    es.forEach(en => {
      if (!en.isIntersecting) return;
      const v = en.target.querySelector("video[data-src]");
      if (v && !v.src) { v.preload = "metadata"; v.src = v.dataset.src; }
      io.unobserve(en.target);
    });
  }, { rootMargin: "400px 0px" });
  grid.querySelectorAll(".am-tile").forEach(t => io.observe(t));
}

function speechPartsWaveform(parts) {
  const timeline = el("div", "lf-waveform");
  timeline.setAttribute("aria-label", "Voiceover loudness timeline");
  const player = el("audio", "lf-wave-player"); player.controls = true;
  const strip = el("div", "lf-wave-strip");
  const total = Math.max(0.01, parts.reduce((sum, part) => sum + (+part.dur || 0), 0));
  parts.forEach((part, order) => {
    const segment = el("button", "lf-wave-segment");
    segment.type = "button";
    segment.style.flexGrow = String(Math.max(0.5, +part.dur || 0));
    segment.title = `Part ${order + 1} · ${clock(+part.dur || 0)}`;
    segment.setAttribute("aria-label", segment.title);
    const canvas = document.createElement("canvas"); canvas.width = 320; canvas.height = 64;
    segment.appendChild(canvas);
    segment.appendChild(el("span", "lf-wave-number", String(order + 1)));
    segment.addEventListener("click", () => {
      strip.querySelectorAll(".lf-wave-segment").forEach(node => node.classList.remove("playing"));
      segment.classList.add("playing"); player.src = part.url || "";
      player.play().catch(() => {});
    });
    strip.appendChild(segment);
    if (!part.url) return;
    fetch(part.url).then(response => response.arrayBuffer()).then(buffer => {
      const Context = window.AudioContext || window.webkitAudioContext;
      if (!Context) return null;
      const context = new Context();
      return context.decodeAudioData(buffer.slice(0)).finally(() => context.close());
    }).then(audio => {
      if (!audio) return;
      const data = audio.getChannelData(0), ctx = canvas.getContext("2d");
      const buckets = canvas.width, step = Math.max(1, Math.floor(data.length / buckets));
      ctx.clearRect(0, 0, canvas.width, canvas.height);
      ctx.fillStyle = part.state === "approved" ? "#8dfc68" : "#6fdc55";
      for (let x = 0; x < buckets; x++) {
        let peak = 0, start = x * step, stop = Math.min(data.length, start + step);
        for (let i = start; i < stop; i++) peak = Math.max(peak, Math.abs(data[i]));
        const h = Math.max(1, Math.pow(peak, .72) * (canvas.height - 8));
        ctx.fillRect(x, (canvas.height - h) / 2, 1, h);
      }
    }).catch(() => { segment.classList.add("wave-unavailable"); });
  });
  player.addEventListener("ended", () => strip.querySelectorAll(".playing").forEach(n => n.classList.remove("playing")));
  timeline.appendChild(strip); timeline.appendChild(player);
  return timeline;
}

async function pollJob() {
  if (!S.jobId) return;
  let d;
  try { d = await jget("/job-status?id=" + encodeURIComponent(S.jobId)); }
  catch (e) { return; }
  if (!d.exists) {
    stopPolling(); S.jobStatus = "missing";
    errorCard(T.err_no_job, ""); persist(); return;
  }
  const kindTitle = $("job-kind-title");
  if (kindTitle) kindTitle.textContent = d.job_kind === "longform"
    ? "Creating your longform video" : "Creating your Short";
  S.jobKind = d.job_kind || "run";
  // The live Chromium image belongs only to a running scrape Clip Short.  It used to poll a
  // process-global endpoint from every processing screen, so a concurrent longform displayed the
  // other run's TikTok/X browser.  The server also verifies the project owner for this job id.
  setScrapePreviewPolling(!!d.scrape_preview_allowed);
  if (d.status !== S.jobStatus) {
    const prev = S.jobStatus;
    S.jobStatus = d.status; renderTopbar(); persist();
    setJobBoxState($("job-card"), d.status);   // recolor the run box: green / orange / red
    // notification chime: render finished, or voiceover ready for approval (halt-after-speech)
    if (prev && prev !== "missing") {
      if (d.status === "done") playNotification("done");
      else if (d.status === "awaiting_approval") playNotification("speech");
    }
  }
  const prog = $("job-progress");
  if (prog && d.progress_html !== lastProgressHTML) { prog.innerHTML = d.progress_html || ""; lastProgressHTML = d.progress_html; }
  const lg = $("job-log");
  if (lg && d.log_text != null && lg.textContent !== d.log_text) { lg.textContent = d.log_text; lg.scrollTop = lg.scrollHeight; }
  renderScreeningPanel(d);
  // (phase-message chat bubbles removed - the user follows progress in the run box / tech log)
  // longform per-part speech approval ("Halt after speech" in the longform creator)
  const lfp = $("job-speech");
  if (lfp && d.status === "awaiting_approval" && (d.lf_parts || []).length) {
    const sig = JSON.stringify((d.lf_parts || []).map(p => [p.index, p.state, p.url]));
    if (lfp.dataset.lfSig !== sig) {
      lfp.dataset.lfSig = sig; lfp.innerHTML = "";
      const m = el("div", "msg assistant"); m.appendChild(el("div", "bubble", esc(T.lf_parts_ready))); lfp.appendChild(m);
      const c = el("div", "chat-card"); lfp.appendChild(c);
      // bulk actions + the total runtime go ABOVE the parts: a long script makes this card
      // thousands of pixels tall, so anything under the last part is effectively unreachable.
      const pending = (d.lf_parts || []).filter(p => p.state === "pending");
      const total = (d.lf_parts || []).reduce((s, p) => s + (+p.dur || 0), 0);
      const head = el("div", "lf-bulk");
      head.appendChild(el("span", "lf-bulk-lbl",
        (d.lf_parts || []).length + " " + (T.lf_parts_word || "parts") +
        (total > 0 ? " · " + clock(total) + " " + (T.lf_total || "total") : "")));
      head.appendChild(el("span", "spacer"));
      if (pending.length > 1) {
        const runAll = async (action, btnEl) => {
          [...head.querySelectorAll("button")].forEach(b => b.disabled = true);
          btnEl.textContent = "…";
          // sequential on purpose: parallel declines would fire N regenerations at the TTS backend
          for (const p of pending) {
            await fetch("/longform-speech-decide?id=" + encodeURIComponent(S.jobId) +
              "&part=" + encodeURIComponent(p.index) + "&action=" + action, { method: "POST" });
          }
          setTimeout(pollJob, 700);
        };
        head.appendChild(btn("✓ " + (T.lf_approve_all || "Approve all") + " (" + pending.length + ")",
          ev => runAll("approve", ev.currentTarget), "primary"));
        head.appendChild(btn("↻ " + (T.lf_decline_all || "Decline & regenerate all"),
          ev => runAll("decline", ev.currentTarget), "danger"));
      }
      c.appendChild(head);
      c.appendChild(speechPartsWaveform(d.lf_parts || []));
      (d.lf_parts || []).forEach(p => {
        const row = el("div", "lf-part");
        row.appendChild(el("div", "card-cap", esc(T.lf_part) + " " + (p.index + 1) +
          (+p.dur > 0 ? " · " + clock(p.dur) : "") +
          (p.state === "approved" ? " · ✓ " + esc(T.lf_approved) :
           p.state === "regenerating" ? " · ↻ " + esc(T.lf_regenerating) : "")));
        row.appendChild(el("div", "lf-part-text", esc((p.text || "").slice(0, 220))));
        if (p.url && p.state !== "regenerating") {
          const au = el("audio", "inline-audio"); au.controls = true; au.src = p.url; row.appendChild(au);
        }
        // Polish the take you already have. Regenerating rolls the dice again and costs another
        // TTS call; a preset fixes level, hiss or tone on the audio that is already sitting
        // there, and "Original" puts it back untouched.
        if (p.state !== "regenerating" && (OPT.voice_presets || []).length) {
          const pol = el("div", "lf-polish");
          pol.appendChild(el("span", "lf-polish-cap", "Polish"));
          const sel = el("select", "lf-polish-select");
          (OPT.voice_presets || []).forEach(o => {
            const opt = new Option(o.label, o.value);
            opt.title = o.hint || "";
            sel.appendChild(opt);
          });
          sel.value = p.preset || "none";
          sel.addEventListener("change", async () => {
            const chosen = sel.value;
            sel.disabled = true;
            const hint = (OPT.voice_presets || []).find(o => o.value === chosen);
            pol.dataset.busy = hint ? hint.label : chosen;
            await fetch("/longform-speech-decide?id=" + encodeURIComponent(S.jobId) +
              "&part=" + encodeURIComponent(p.index) + "&action=polish" +
              "&preset=" + encodeURIComponent(chosen), { method: "POST" });
            setTimeout(pollJob, 900);
          });
          pol.appendChild(sel);
          const chosen = (OPT.voice_presets || []).find(o => o.value === (p.preset || "none"));
          if (chosen && chosen.hint) pol.appendChild(el("span", "lf-polish-hint", esc(chosen.hint)));
          row.appendChild(pol);
        }
        if (p.state === "pending") {
          const foot = el("div", "card-foot");
          const decide = async (action, btnEl) => {
            btnEl.disabled = true;
            await fetch("/longform-speech-decide?id=" + encodeURIComponent(S.jobId) +
              "&part=" + encodeURIComponent(p.index) + "&action=" + action, { method: "POST" });
            setTimeout(pollJob, 700);
          };
          foot.appendChild(btn("✓ " + T.lf_approve, ev => decide("approve", ev.currentTarget), "primary"));
          foot.appendChild(btn("↻ " + T.lf_decline, ev => decide("decline", ev.currentTarget), "danger"));
          row.appendChild(foot);
        }
        c.appendChild(row);
      });
      scrollDown();
    }
  } else if (lfp && lfp.dataset.lfSig && d.status !== "awaiting_approval") {
    lfp.innerHTML = ""; delete lfp.dataset.lfSig;
  }
  // Discovery mode: pick ONE of the found topic/material candidates
  const dpHost = $("job-speech");
  // (screening panel renders above; picker below)
  if (dpHost) {
    const cands = d.discovery_review || [];
    if (d.status === "awaiting_approval" && cands.length && !dpHost.dataset.discDone) {
      dpHost.dataset.discDone = "1"; dpHost.innerHTML = "";
      const c = el("div", "chat-card speech-approve-card"); dpHost.appendChild(c);
      c.appendChild(el("div", "sa-head",
        `<div class="sa-title"><b>Pick the topic &amp; material</b><em>Discovery found ${cands.length} long source videos. Pick ONE — only then the script and voiceover are produced.</em></div>`));
      const rail = el("div", "cand-rail-wrap");
      const grid = el("div", "");
      // grid (not flex-wrap): every candidate column is the SAME height, so the pick
      // buttons sit on one baseline instead of three (user UI review 2026-07-25)
      // ONE scrolling row instead of two stacked rows (UI review 2026-07-25): a peeking
      // next card is the affordance that the row scrolls, and scroll-snap keeps a card
      // aligned after every swipe / arrow click.
      grid.className = "cand-rail";
      cands.forEach(cd => {
        const card = el("div", "");
        card.className = "cand-card";
        card.appendChild(el("strong", "", esc(cd.title || "Candidate")));
        card.appendChild(el("div", "card-note", `@${esc(cd.author || "")} · ${cd.dur}s · ${(+cd.likes || 0).toLocaleString()} likes · appeal ${cd.appeal}/10`));
        if (cd.premise) card.appendChild(el("div", "card-note", "“" + esc(cd.premise) + "”"));
        if (cd.video_url) {
          const vp = document.createElement("video");
          vp.src = cd.video_url; vp.controls = true; vp.preload = "metadata";
          vp.className = "cand-video";
          card.appendChild(vp);
        } else if (cd.sheet_url) {
          const im = document.createElement("img");
          im.src = cd.sheet_url; im.style.cssText = "width:100%; border-radius:8px; margin:6px 0;";
          card.appendChild(im);
        }
        const ol = el("ol", "");
        ol.style.cssText = "margin:4px 0 8px 16px; color:var(--muted); font-size:11.5px; max-height:104px; overflow-y:auto;";
        (cd.stages || []).slice(0, 6).forEach(s => ol.appendChild(el("li", "", esc(s))));
        card.appendChild(ol);
        const pickBtn = btn("Use candidate " + ((+cd.index || 0) + 1), async () => {
          await fetch("/approve-discovery?id=" + encodeURIComponent(S.jobId)
                      + "&action=pick&choice=" + (+cd.index || 0), { method: "POST" });
          dpHost.innerHTML = ""; delete dpHost.dataset.discDone;
        }, "primary");
        pickBtn.style.marginTop = "auto";     // bottom-aligned in every column
        pickBtn.style.width = "100%";
        card.appendChild(pickBtn);
        grid.appendChild(card);
      });
      rail.appendChild(grid);
      [["prev", "Scroll to previous candidates", "M15 6l-6 6 6 6"],
       ["next", "Scroll to more candidates", "M9 6l6 6-6 6"]].forEach(([dir, label, path]) => {
        const b = el("button", "cand-nav cand-nav-" + dir);
        b.type = "button"; b.setAttribute("aria-label", label); b.title = label;
        b.innerHTML = '<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="' + path + '"/></svg>';
        b.addEventListener("click", () => {
          const step = (grid.querySelector(".cand-card") || {}).offsetWidth || 240;
          grid.scrollBy({ left: (dir === "next" ? 1 : -1) * (step + 12),
                          behavior: REDUCED ? "auto" : "smooth" });
        });
        rail.appendChild(b);
      });
      const syncNav = () => {
        const max = grid.scrollWidth - grid.clientWidth - 2;
        rail.querySelector(".cand-nav-prev").hidden = grid.scrollLeft <= 2;
        rail.querySelector(".cand-nav-next").hidden = grid.scrollLeft >= max;
      };
      grid.addEventListener("scroll", syncNav, { passive: true });
      requestAnimationFrame(syncNav);
      c.appendChild(rail);
      scrollDown();
    } else if (dpHost.dataset.discDone && d.status !== "awaiting_approval") {
      dpHost.innerHTML = ""; delete dpHost.dataset.discDone;
    }
  }
  // speech approval
  const sp = $("job-speech");
  if (sp) {
    if (d.status === "awaiting_approval" && d.motion_manual && !sp.dataset.motionManual) {
      sp.dataset.motionManual = "1"; sp.innerHTML = "";
      const chapter = +(d.motion_manual.chapter || 0);
      const c = el("div", "chat-card speech-approve-card motion-preflight-card"); sp.appendChild(c);
      c.appendChild(el("div", "sa-head", `<div class="sa-title"><b>Generate chapter ${chapter} of 3 in normal Chrome</b><em>Higgsfield stays completely manual: keep Unlimited on, paste this prompt, generate and download the finished clip. ShortsLab only imports it and prepares the next continuity frame.</em></div>`));
      const prompt = el("textarea", "sa-textarea"); prompt.readOnly = true; prompt.rows = 10; prompt.value = String(d.motion_manual.prompt || ""); c.appendChild(prompt);
      const promptActions = el("div", "sa-actions");
      promptActions.appendChild(btn("Copy prompt", async ev => {
        const b = ev.currentTarget;
        try {
          if (navigator.clipboard && navigator.clipboard.writeText) await navigator.clipboard.writeText(prompt.value);
          else { prompt.focus(); prompt.select(); document.execCommand("copy"); }
          b.textContent = "Copied";
          setTimeout(() => { b.textContent = "Copy prompt"; }, 1400);
        } catch (_err) {
          prompt.focus(); prompt.select();
          b.textContent = "Select & copy";
        }
      }, "secondary"));
      c.appendChild(promptActions);
      if (d.motion_manual.first_frame_url) {
        const frame = el("div", "motion-frame-note");
        frame.innerHTML = `<b>Use this as Higgsfield's first frame for chapter ${chapter}.</b>`;
        const img = document.createElement("img"); img.src = d.motion_manual.first_frame_url; img.alt = "Previous chapter final frame"; img.className = "motion-continuity-frame";
        const download = document.createElement("a"); download.href = d.motion_manual.first_frame_url; download.download = `ai-motion-chapter-${chapter}-first-frame.png`; download.textContent = "Download continuity frame";
        frame.appendChild(img); frame.appendChild(download); c.appendChild(frame);
      } else {
        c.appendChild(el("p", "motion-frame-note", "Chapter 1 is prompt-only unless you supplied an optional first-frame image when creating the project."));
      }
      const picker = document.createElement("input"); picker.type = "file"; picker.accept = "video/mp4,video/quicktime,video/webm"; picker.className = "sa-file"; c.appendChild(picker);
      const actions = el("div", "sa-actions");
      const submit = btn("Import finished chapter", async ev => {
        if (!picker.files || !picker.files[0]) { picker.focus(); return; }
        const b = ev.currentTarget; b.disabled = true; b.textContent = "Importing chapter…";
        const fd = new FormData(); fd.append("motion_manual_clip", picker.files[0]);
        try {
          const res = await fetch("/motion-manual-upload?id=" + encodeURIComponent(S.jobId), { method: "POST", body: fd });
          const out = await res.json().catch(() => ({}));
          if (!res.ok || !out.ok) throw new Error(out.error || "could not import that video");
        } catch (err) {
          b.disabled = false; b.textContent = "Import finished chapter"; alert(String(err.message || err));
        }
      }, "primary");
      actions.appendChild(submit); c.appendChild(actions); scrollDown();
    } else if ((d.status !== "awaiting_approval" || !d.motion_manual) && sp.dataset.motionManual) {
      sp.innerHTML = ""; delete sp.dataset.motionManual;
    }
    if (d.status === "awaiting_approval" && d.motion_preflight && !sp.dataset.motionReady) {
      sp.dataset.motionReady = "1"; sp.innerHTML = "";
      const c = el("div", "chat-card speech-approve-card motion-preflight-card"); sp.appendChild(c);
      c.appendChild(el("div", "sa-head", `<div class="sa-title"><b>Prepare Higgsfield yourself</b><em>The browser is on the Higgsfield homepage. Navigate to Seedance 2.5, set 10 seconds, 9:16 and Unlimited, complete any verification, then confirm here. ShortsLab will not navigate or change any Higgsfield setting.</em></div>`));
      const actions = el("div", "sa-actions");
      actions.appendChild(btn("I’m ready — start AI Motion", async ev => {
        const b = ev.currentTarget; b.disabled = true; b.textContent = "Checking Higgsfield…";
        const res = await fetch("/motion-preflight-ready?id=" + encodeURIComponent(S.jobId), { method: "POST" });
        if (!res.ok) { b.disabled = false; b.textContent = "I’m ready — start AI Motion"; }
      }, "primary"));
      c.appendChild(actions); scrollDown();
    } else if ((d.status !== "awaiting_approval" || !d.motion_preflight) && sp.dataset.motionReady) {
      sp.innerHTML = ""; delete sp.dataset.motionReady;
    }
    if (d.status === "awaiting_approval" && d.physics_preview_url && !sp.dataset.physDone) {
      sp.dataset.physDone = "1"; sp.innerHTML = "";
      const c = el("div", "chat-card speech-approve-card"); sp.appendChild(c);
      c.appendChild(el("div", "sa-head",
        `<div class="sa-title"><b>${esc(T.physics_approve_t)}</b><em>${esc(T.physics_approve_d)}</em></div>`));
      const shot = el("img", "physics-preview-shot");
      shot.src = d.physics_preview_url;
      shot.alt = "";
      c.appendChild(shot);
      // What is actually about to be rendered. "Does this look right" cannot be answered
      // from a still alone - a wrong scene and a wrong mass look equally plausible.
      const pk = d.physics_pick;
      if (pk) {
        const facts = el("div", "phys-pickfacts");
        const chips = [`<b>${esc(pk.title || pk.scene || "")}</b>`];
        if (pk.sweep && (pk.sweep.values || []).length) {
          chips.push(`<span>${esc(String(pk.sweep.param || "").replace(/_/g, " "))}: ` +
                     `${esc((pk.sweep.values || []).join(" · "))}${esc(pk.sweep.unit || "")}</span>`);
        }
        const sweptKey = pk.sweep ? pk.sweep.param : "";
        Object.keys(pk.params || {}).filter(k => k !== sweptKey).slice(0, 8).forEach(k => {
          chips.push(`<span>${esc(k.replace(/_/g, " "))} ` +
                     `<i>${esc(String(pk.params[k]).replace(/_/g, " "))}</i></span>`);
        });
        facts.innerHTML = chips.join("");
        c.appendChild(facts);
      }
      const actions = el("div", "sa-actions");
      const decide = async (action, b, label) => {
        b.disabled = true; b.textContent = "…";
        try {
          await fetch("/approve-physics?id=" + encodeURIComponent(S.jobId) + "&action=" + action,
                      { method: "POST" });
          if (action === "retry") { sp.innerHTML = ""; delete sp.dataset.physDone; }
        } catch (e) { b.disabled = false; b.textContent = label; }
      };
      actions.appendChild(btn(T.physics_approve_yes,
        ev => decide("approve", ev.currentTarget, T.physics_approve_yes), "primary"));
      // The seed alone changes the whole take. Re-rolling it beats cancelling the run and
      // filling the form in again, which was the only way to see a second option.
      actions.appendChild(btn("Another take",
        ev => decide("retry", ev.currentTarget, "Another take"), "secondary"));
      actions.appendChild(btn(T.physics_approve_no,
        ev => decide("decline", ev.currentTarget, T.physics_approve_no), "danger"));
      c.appendChild(actions); scrollDown();
    } else if ((d.status !== "awaiting_approval" || !d.physics_preview_url) && sp.dataset.physDone) {
      sp.innerHTML = ""; delete sp.dataset.physDone;
    }
    if (d.status === "awaiting_approval" && d.speech_audio_url && !sp.dataset.done) {
      sp.dataset.done = "1"; sp.innerHTML = "";
      const c = el("div", "chat-card speech-approve-card"); sp.appendChild(c);
      c.appendChild(el("div", "sa-head",
        `<span class="sa-ico" aria-hidden="true"><svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2a3 3 0 0 0-3 3v6a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3z"/><path d="M19 10v1a7 7 0 0 1-14 0v-1M12 18v4"/></svg></span>` +
        `<div class="sa-title"><b>Your voiceover is ready</b><em>Listen, then approve — or redo it with a different voice.</em></div>`));
      const au = el("audio", "sa-audio"); au.controls = true; au.src = d.speech_audio_url; c.appendChild(au);
      // narration speed: the preview is baked at `curSpeed`; picking a different one previews it
      // live (audio playbackRate = chosen/current) and re-tempos the voice on approve.
      const curSpeed = +(d.speech_speed || 0) || 1.15;
      const speedRow = el("div", "sa-field");
      speedRow.appendChild(el("label", "sa-lbl", "Narration speed"));
      const ssel = el("select");
      // A Short is always pushed faster than life, so its list only climbs. A longform narration
      // is the opposite problem - it can easily be too brisk over 15 minutes - so it also gets
      // the slower end.
      const steps = S.flow === "longform"
        ? ["0.9", "0.95", "1.0", "1.05", "1.1", "1.15", "1.2", "1.3"]
        : ["1.0", "1.15", "1.2", "1.3", "1.4", "1.5", "1.6"];
      ssel.appendChild(new Option("Keep current (" + curSpeed.toFixed(2) + "x)", ""));
      steps.forEach(v => ssel.appendChild(new Option((+v).toFixed(2) + "x", v)));
      ssel.addEventListener("change", () => {
        const sel = +ssel.value || curSpeed;
        au.playbackRate = Math.max(0.5, Math.min(2.5, sel / curSpeed));
        try { au.currentTime = 0; au.play().catch(() => {}); } catch (e) {}
      });
      speedRow.appendChild(ssel); c.appendChild(speedRow);
      // primary action
      const approveRow = el("div", "sa-actions");
      approveRow.appendChild(btn("✓ " + T.approve_continue, async () => {
        const q = ssel.value ? "&speed=" + encodeURIComponent(ssel.value) : "";
        await fetch("/approve-speech?id=" + encodeURIComponent(S.jobId) + q, { method: "POST" });
        sp.innerHTML = ""; delete sp.dataset.done;
      }, "primary"));
      c.appendChild(approveRow);
      // Redo is clip-only: /replace-speech restarts the run with a new speaker, which longform
      // has no path for (its narrator lives on the job, and its parts were already approved one
      // by one on the card above). Longform stops at the player + speed.
      if (S.flow === "longform") { scrollDown(); return; }
      // redo section (voice + model + new take), visually subordinate
      const redo = el("div", "sa-redo");
      redo.appendChild(el("div", "sa-lbl", "Not happy? Redo with a different voice"));
      const redoRow = el("div", "sa-redo-row");
      const msel = el("select"); OPT.tts_model.forEach(o => msel.appendChild(new Option(o.label, o.value)));
      if (S.values.tts_model && [...msel.options].some(o => o.value === S.values.tts_model)) msel.value = S.values.tts_model;
      const vsel = el("select");
      const seedHost = el("div", "sa-seed-settings");
      const refreshRedoTts = () => {
        const model = msel.value;
        S.values.tts_model = model;
        const choices = ttsVoiceOptions(model);
        vsel.innerHTML = "";
        choices.forEach(o => vsel.appendChild(new Option(o.label || o.value || o, o.value || o)));
        resetTtsVoice(S.values);
        vsel.value = S.values.tts_voice;
        seedHost.innerHTML = "";
        const settings = seedTtsSettings(S.values);
        if (settings) seedHost.appendChild(settings);
        persist();
      };
      vsel.addEventListener("change", () => { S.values.tts_voice = vsel.value; persist(); });
      msel.addEventListener("change", refreshRedoTts);
      refreshRedoTts();
      redoRow.appendChild(vsel); redoRow.appendChild(msel);
      redoRow.appendChild(btn("↻ " + T.new_take, async (ev) => {
        const b = ev.currentTarget; b.disabled = true;
        const body = new URLSearchParams({ speaker_name: S.values.speaker_name || "Narrator",
          tts_voice: vsel.value, tts_model: msel.value });
        ["tts_voice_instruction", "tts_language", "tts_native_speed", "tts_volume",
          "tts_pitch", "tts_sample_rate", "tts_output_format"].forEach(k => {
            if (S.values[k] != null) body.set(k, S.values[k]);
          });
        // #127 - /replace-speech regenerates the voiceover as a follow-on run in the SAME project.
        S.replacePending = true;
        try {
          const r = await fetch("/replace-speech?id=" + encodeURIComponent(S.jobId) + "&json=1",
            { method: "POST", headers: { "Content-Type": "application/x-www-form-urlencoded" }, body });
          const d = await r.json();
          if (d && d.id) { S.jobId = d.id; S.jobStatus = "running"; persist(); }
        } catch (e) {
          try {
            const jl = await (await fetch("/jobs-list")).json();
            const run = (jl.jobs || []).find(j => j.status === "running" || j.status === "awaiting_approval");
            if (run) { S.jobId = run.id; S.jobStatus = "running"; persist(); }
          } catch (e2) {}
        }
        S.replacePending = false;
        sp.innerHTML = ""; delete sp.dataset.done;
        renderTopbar();
      }, "ghost"));
      redo.appendChild(redoRow); redo.appendChild(seedHost); c.appendChild(redo);
      scrollDown();
    } else if (d.status !== "awaiting_approval" && sp.dataset.done) {
      sp.innerHTML = ""; delete sp.dataset.done;
    }
  }
  // assigned footage only (clean grid; the full grouped media wall stays on ?legacy_ui=1)
  renderLiveProcessingTimeline(d.live_timeline || null);
  renderAssignedMedia(d.assigned_media || []);
  // The raw grouped "outputs" fragment is NOT shown in the chat any more - on done we render a
  // single clean result card (video + download). Keep the latest outputs_html only as the source
  // renderResultCard parses the final video out of.
  lastOutputsHTML = d.outputs_html || lastOutputsHTML || "";
  // a voiceover "New take" intentionally cancels the paused job and hands over to a follow-on
  // run - never render that hand-off as an aborted run while the new job id is being adopted
  if (d.status === "cancelled" && S.replacePending) return;
  if (["done", "error", "cancelled"].includes(d.status)) {
    stopPolling();
    const cancelB = $("job-cancel"); if (cancelB) cancelB.remove();
    if (!$("job-final")) {
      const fin = el("div"); fin.id = "job-final"; chat.appendChild(fin);
      if (d.status === "done") {
        // clip-short skips the final render and jumps straight into the timeline editor
        if (d.speech_review) {
          const c = el("div", "chat-card"); fin.appendChild(c);
          c.appendChild(el("h3", "", "Speech parts saved"));
          c.appendChild(el("div", "card-note",
            "The regenerated narration was transcribed again. Image-switch timings, image filenames and the timeline now follow the new speech."));
          const foot = el("div", "card-foot"); foot.appendChild(el("span", "spacer"));
          foot.appendChild(btn("Back to project", () => {
            S.jobId = null; S.jobStatus = ""; renderAll(); persist();
          }, "primary"));
          c.appendChild(foot);
        } else if (d.thumbnail_generation && d.project_slug) {
          const m = el("div", "msg assistant");
          m.appendChild(el("div", "bubble", "Three thumbnail and title options are ready."));
          fin.appendChild(m); scrollDown();
          setTimeout(() => openLongformThumbnailResults(d.project_slug), 350);
        } else if (d.open_longform_editor && d.project_slug) {
          const m = el("div", "msg assistant");
          m.appendChild(el("div", "bubble", "All images are ready - opening the pre-render timeline before rendering..."));
          fin.appendChild(m); scrollDown();
          setTimeout(() => openLongformFrameEditor(d.project_slug), 500);
        } else if (d.open_timeline && d.project_slug) {
          const m = el("div", "msg assistant");
          m.appendChild(el("div", "bubble", "✓ Your edit is ready — opening the timeline editor…"));
          fin.appendChild(m); scrollDown();
          setTimeout(() => openTimelineWithLoading(d.project_slug, S.projectTitle || d.project_slug), 750);
        } else {
          const m = el("div", "msg assistant"); m.appendChild(el("div", "bubble", "✓ " + esc(T.your_short_ready))); fin.appendChild(m);
          renderResultCard(fin, d);
        }
      } else if (d.status === "error") {
        const c = el("div", "chat-card err-card"); fin.appendChild(c);
        c.appendChild(el("h3", "", esc(T.job_error)));
        if (d.error_html) c.appendChild(el("div", "embed", d.error_html));
        const foot = el("div", "card-foot");
        foot.appendChild(btn(T.continue_project, () => S.projectSlug ? continueProject(S.projectSlug) : resetToMode(), "small"));
        foot.appendChild(btn(T.new_chat, resetToMode, "ghost small"));
        c.appendChild(foot);
      } else {
        // cancelled: one compact, intentional card (not a big empty box with a lone button)
        const c = el("div", "chat-card end-card end-cancelled"); fin.appendChild(c);
        c.appendChild(el("div", "end-body",
          `<span class="end-ico" aria-hidden="true"><svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="9"/><path d="m5.6 5.6 12.8 12.8"/></svg></span>` +
          `<div class="end-copy"><b>Run cancelled</b><em>The run was stopped before it finished.</em></div>`));
        const foot = el("div", "card-foot");
        foot.appendChild(el("span", "spacer"));
        foot.appendChild(btn("＋ " + T.new_chat, resetToMode, "primary small"));
        c.appendChild(foot);
      }
      scrollDown();
    }
    refreshSidebar();
  }
}
function renderResultCard(container, d) {
  // find the final video inside the (already-rendered) outputs fragment
  const tmp = el("div", "", d.outputs_html || lastOutputsHTML || "");
  const vid = tmp.querySelector("video");
  const c = el("div", "chat-card"); container.appendChild(c);
  const wrap = el("div", "result-wrap"); c.appendChild(wrap);
  if (vid) {
    const v = el("video", "result-video"); v.controls = true; v.src = vid.getAttribute("src");
    v.setAttribute("playsinline", ""); wrap.appendChild(v);
  }
  const side = el("div", "result-side"); wrap.appendChild(side);
  if (vid) {
    const fileUrl = vid.getAttribute("src") || "";
    const raw = decodeURIComponent(fileUrl.split("path=")[1] || "");
    const name = (raw.split(/[\\/]/).pop()) || "short.mp4";
    side.appendChild(btn("⬇ " + T.download, (ev) => downloadVideo(fileUrl, raw, name, ev.currentTarget), "primary"));
  }
  if (d.job_kind === "longform" && d.project_slug)
    renderLongformPostRender(c, d.project_slug);
  jget("/jobs-list").then(dd => {
    const j = (dd.jobs || []).find(x => x.id === S.jobId);
    if (!j || !j.project_slug) return;
    if ((j.kind || d.job_kind) === "longform")
      side.appendChild(btn("Open timeline", () => openLongformFrameEditor(j.project_slug), "secondary"));
    else
      side.appendChild(linkBtn("🎞 " + T.open_timeline, "/timeline?slug=" + encodeURIComponent(j.project_slug)));
  }).catch(() => {});
  side.appendChild(btn(T.new_project, resetToMode, "ghost"));
  side.appendChild(btn(T.nav_assets, () => showAssets(false), "ghost"));
}

async function renderLongformPostRender(card, slug) {
  let panel = card.querySelector(".lf-postrender");
  if (!panel) { panel = el("section", "lf-postrender"); card.appendChild(panel); }
  panel.innerHTML = `<div class="lf-postrender-head"><div><span>THUMBNAILS</span><h3>Choose the packaging</h3></div><p>One focused image, one matching video title.</p></div><div class="lf-postrender-loading">Loading thumbnail options...</div>`;
  let data;
  try { data = await jget("/longform-frames?slug=" + encodeURIComponent(slug)); } catch (e) { data = null; }
  if (!data || !data.ok) { panel.querySelector(".lf-postrender-loading").textContent = "Could not load thumbnail options."; return; }
  const loading = panel.querySelector(".lf-postrender-loading"); if (loading) loading.remove();
  const grid = el("div", "lf-postrender-grid"); panel.appendChild(grid);
  const variants = data.thumbnails || [];
  if (!variants.length) {
    const empty = el("div", "lf-empty-state", "No thumbnail set exists for this render yet.");
    empty.appendChild(btn("Generate 3 thumbnails + titles", async ev => {
      ev.currentTarget.disabled = true; const r = await jpost("/longform-thumbnail", {slug});
      if (r && r.ok && r.id) startJob(r.id); else errorCard(T.err_generic, (r && r.error) || "Could not start generation.");
    }, "secondary small")); panel.appendChild(empty); return;
  }
  variants.forEach(t => {
    const option = el("article", "lf-postrender-option" + (t.selected ? " selected" : ""));
    const image = el("img"); image.src=t.img; image.alt="Thumbnail option " + (t.index+1); option.appendChild(image);
    const copy = el("div"); option.appendChild(copy);
    if(t.selected) copy.appendChild(el("span","lf-selected-pill","SELECTED"));
    copy.appendChild(el("h4","",esc(t.title||"Untitled video")));
    if(!t.selected) copy.appendChild(btn("Use this pair",async ev=>{
      ev.currentTarget.disabled=true; const r=await jpost("/longform-thumbnail-select",{slug,index:t.index});
      if(!r||!r.ok){ev.currentTarget.disabled=false;errorCard(T.err_generic,(r&&r.error)||"Selection failed.");return;}
      renderLongformPostRender(card,slug);
    },"ghost small"));
    grid.appendChild(option);
  });
}

/* ---------------------------------------------------- generated thumbnail/title results */
async function openLongformThumbnailResults(slug) {
  document.querySelectorAll(".lf-thumbnail-results,.lf-frame-editor").forEach(x => x.remove());
  let data;
  try { data = await jget("/longform-frames?slug=" + encodeURIComponent(slug)); } catch (e) { data = null; }
  if (!data || !data.ok) { errorCard(T.err_generic, (data && data.error) || "Could not load thumbnails."); return; }
  const root = el("div", "chat-card lf-thumbnail-results"); chat.appendChild(root);
  const head = el("div", "lf-editor-head"); root.appendChild(head);
  const copy = el("div"); head.appendChild(copy);
  copy.appendChild(el("div", "lf-eyebrow", "THUMBNAIL RESULTS"));
  copy.appendChild(el("h2", "", "Choose your thumbnail + title"));
  copy.appendChild(el("p", "", "All three GPT Image 2.0 results are shown with the title created specifically for that concept."));
  head.appendChild(btn("Close", () => root.remove(), "ghost small"));
  const list = el("div", "lf-thumbnail-options lf-results-grid"); root.appendChild(list);
  const variants = data.thumbnails || [];
  if (!variants.length) {
    list.appendChild(el("div", "lf-empty-state", "No completed thumbnail variants were found."));
  } else variants.forEach(t => {
    const card = el("article", "lf-thumbnail-card" + (t.selected ? " selected" : ""));
    const image = el("img"); image.src = t.img + (t.img.includes("?") ? "&" : "?") + "v=" + Date.now();
    image.alt = "Thumbnail option " + (t.index + 1); card.appendChild(image);
    const body = el("div", "lf-thumbnail-copy"); card.appendChild(body);
    body.appendChild(el("span", "lf-option-label", t.selected ? "SELECTED" : "OPTION " + (t.index + 1)));
    body.appendChild(el("h4", "", esc(t.title || "Untitled concept")));
    if (!t.selected) body.appendChild(btn("Use this thumbnail + title", async ev => {
      ev.currentTarget.disabled = true;
      const result = await jpost("/longform-thumbnail-select", {slug:data.slug,index:t.index});
      if (!result || !result.ok) { ev.currentTarget.disabled = false; errorCard(T.err_generic, (result && result.error) || "Selection failed."); return; }
      root.remove(); openLongformThumbnailResults(data.slug);
    }, "primary small"));
    card.appendChild(body); list.appendChild(card);
  });
  const foot = el("div", "card-foot lf-results-foot");
  foot.appendChild(btn("Generate 3 new options", async ev => {
    ev.currentTarget.disabled = true;
    const result = await jpost("/longform-thumbnail", {slug:data.slug});
    if (result && result.ok && result.id) { root.remove(); startJob(result.id); }
    else { ev.currentTarget.disabled = false; errorCard(T.err_generic, (result && result.error) || "Could not start generation."); }
  }, "secondary"));
  foot.appendChild(el("span", "spacer"));
  foot.appendChild(btn("Open timeline", () => { root.remove(); openLongformFrameEditor(data.slug); }, "primary"));
  root.appendChild(foot);
  requestAnimationFrame(() => root.scrollIntoView({behavior:REDUCED ? "auto" : "smooth", block:"start"}));
}

/* ---------------------------------------------------- Sketch Explainer pre-render timeline */
async function openLongformPreRenderEditor(slug) {
  document.querySelectorAll(".lf-frame-editor").forEach(x => x.remove());
  let data;
  try { data = await jget("/longform-frames?slug=" + encodeURIComponent(slug)); } catch (e) { data = null; }
  if (!data || !data.ok) { errorCard(T.err_generic, (data && data.error) || "Could not load the timeline."); return; }
  // Longform is an NLE, not a chat card.  Keeping this inside `.chat-scroll` made a 16-minute
  // edit compete with the conversation's narrow column and buried the Render action below the
  // fold.  It is an overlay like the normal Clip Short editor: the underlying chat stays intact
  // and Close simply returns to it.
  const closeEditor = () => { root.remove(); document.body.classList.remove("lf-editor-open"); };
  const root = el("div", "lf-frame-editor lf-prerender lf-fullscreen-editor");
  document.body.appendChild(root); document.body.classList.add("lf-editor-open");
  const head = el("div", "lf-editor-head"); root.appendChild(head);
  const hc = el("div"); head.appendChild(hc);
  hc.appendChild(el("div", "lf-eyebrow", "PRE-RENDER REVIEW"));
  hc.appendChild(el("h2", "", "Shape the final Sketch Explainer"));
  hc.appendChild(el("p", "", "Move images against the fixed narration, recover unused generations and choose the thumbnail before rendering."));
  head.appendChild(btn("Close", closeEditor, "ghost small"));
  const thumbs = el("section", "lf-section lf-thumbnail-section"); root.appendChild(thumbs);
  // The opener is the first four seconds of the finished short and the one part the user
  // generates outside the app, so it sits above the timeline rather than below it.
  const opener = el("section", "lf-section lf-opener-section"); root.appendChild(opener);
  const timeline = el("section", "lf-section lf-timeline-section"); root.appendChild(timeline);
  const unused = el("section", "lf-section lf-unused-section"); root.appendChild(unused);
  const audio = el("audio", "lf-voice-player"); audio.controls = true; audio.preload = "metadata";
  // "4" is four pixels per second - built for a twenty-minute longform. On a 34.7s short that
  // is 139px of clips inside a canvas floored at 900px: 15% content, 85% empty, with tiles too
  // small to grab. The default now depends on how long the video actually is.
  let swapFrom = null, armedUnused = null, selectedFrame = 0, timelineScale = 4, zoomChoice = null;

  async function refresh() {
    const next = await jget("/longform-frames?slug=" + encodeURIComponent(data.slug));
    if (next && next.ok) { data = next; paintAll(); }
  }
  async function place(payload, idx) {
    let r = null;
    if (payload.kind === "unused") r = await jpost("/longform-frame-use", { slug: data.slug, rel_path: payload.rel_path, idx });
    if (payload.kind === "frame" && Number(payload.idx) !== Number(idx))
      r = await jpost("/longform-frame-swap", { slug: data.slug, a: payload.idx, b: idx });
    swapFrom = null; armedUnused = null;
    if (r && !r.ok) errorCard(T.err_generic, r.error || "Could not move the image.");
    await refresh();
  }
  function sectionHead(title, note, aside) {
    const bar = el("div", "lf-section-head"), copy = el("div");
    copy.appendChild(el("h3", "", title)); copy.appendChild(el("p", "", note)); bar.appendChild(copy);
    if (aside) bar.appendChild(aside); return bar;
  }
  function paintOpener() {
    opener.innerHTML = "";
    if (!data.opener_enabled) { opener.style.display = "none"; return; }
    opener.style.display = "";
    const have = !!data.opener_clip;
    opener.appendChild(sectionHead(
      "Opening clip",
      have ? "Your generated opener is in. It plays before the first drawing."
           : "Generate this from the prompt below, then drop the file in - until you do, the built-in Google clip is used.",
      el("span", "lf-count", have ? (Number(data.opener_seconds || 0).toFixed(1) + "s") : "not yet")));

    const row = el("div", "lf-opener-row"); opener.appendChild(row);
    const slot = el("div", "lf-opener-slot" + (have ? " filled" : " empty"));
    if (have) {
      const v = el("video", "lf-opener-video"); v.src = data.opener_clip; v.controls = true;
      v.preload = "metadata"; slot.appendChild(v);
    } else {
      slot.appendChild(el("div", "lf-opener-ph",
        "PLACEHOLDER<span>~" + Number(data.opener_expected_seconds || 4).toFixed(0) + "s opener</span>"));
    }
    row.appendChild(slot);

    const side = el("div", "lf-opener-side"); row.appendChild(side);
    if (data.opener_query) side.appendChild(el("p", "lf-opener-query", "Search line: " + esc(data.opener_query)));
    const promptBox = el("textarea", "lf-opener-prompt");
    promptBox.readOnly = true; promptBox.rows = 8;
    promptBox.value = data.opener_prompt || "No prompt was written for this project - it predates the opener prompt, or the hook intro was off.";
    side.appendChild(promptBox);

    const tools = el("div", "lf-opener-tools"); side.appendChild(tools);

    // Shared by the file picker and by dropping onto the slot. The panel says "drop the file
    // in", so the placeholder has to actually accept one.
    async function sendOpener(file) {
      if (!file) return;
      if (!/\.(mp4|mov|webm|mkv)$/i.test(file.name || "")) {
        alert("Please use a video file (.mp4, .mov, .webm, .mkv).");
        return;
      }
      const fd = new FormData(); fd.append("slug", data.slug); fd.append("file", file);
      slot.classList.add("busy"); tools.classList.add("busy");
      try {
        const r = await (await fetch("/longform-opener-upload", {method: "POST", body: fd})).json();
        if (!r.ok) alert(r.error || "Upload failed.");
      } catch (e) { alert("Upload failed."); }
      slot.classList.remove("busy"); tools.classList.remove("busy");
      await refresh();
    }

    // dragover must be cancelled or the browser navigates to the dropped file instead.
    let dragDepth = 0;
    slot.addEventListener("dragenter", ev => {
      ev.preventDefault(); dragDepth += 1; slot.classList.add("drop-target");
    });
    slot.addEventListener("dragover", ev => { ev.preventDefault(); ev.dataTransfer.dropEffect = "copy"; });
    slot.addEventListener("dragleave", () => {
      dragDepth = Math.max(0, dragDepth - 1);
      if (!dragDepth) slot.classList.remove("drop-target");
    });
    slot.addEventListener("drop", async ev => {
      ev.preventDefault(); dragDepth = 0; slot.classList.remove("drop-target");
      const files = (ev.dataTransfer && ev.dataTransfer.files) || [];
      if (files.length) await sendOpener(files[0]);
    });
    slot.addEventListener("click", ev => { if (!have) pick.click(); });
    slot.title = have ? "Drop a video here to replace this opener"
                      : "Click, or drop your generated clip here";
    tools.appendChild(btn("Copy prompt", async ev => {
      try { await navigator.clipboard.writeText(promptBox.value); ev.currentTarget.textContent = "Copied"; }
      catch (e) { promptBox.select(); ev.currentTarget.textContent = "Select + copy"; }
      setTimeout(() => { ev.currentTarget.textContent = "Copy prompt"; }, 1800);
    }, "small"));
    const pick = el("input"); pick.type = "file";
    pick.accept = "video/mp4,video/quicktime,video/webm,video/x-matroska,.mp4,.mov,.webm,.mkv";
    pick.style.display = "none";
    pick.addEventListener("change", async () => {
      if (pick.files && pick.files.length) await sendOpener(pick.files[0]);
    });
    side.appendChild(pick);
    tools.appendChild(btn(have ? "Replace clip" : "Insert generated clip", () => pick.click(), "small"));
    if (have) tools.appendChild(btn("Remove", async () => {
      const fd = new FormData(); fd.append("slug", data.slug); fd.append("remove", "1");
      try { await fetch("/longform-opener-upload", {method: "POST", body: fd}); } catch (e) {}
      await refresh();
    }, "ghost small"));
  }

  function paintThumbs() {
    thumbs.innerHTML = "";
    const gen = btn((data.thumbnails || []).length ? "Generate new thumbnails" : "Generate thumbnails", async ev => {
      ev.currentTarget.disabled = true;
      const r = await jpost("/longform-thumbnail", { slug: data.slug });
      if (r && r.ok && r.id) { root.remove(); startJob(r.id); }
      else { ev.currentTarget.disabled = false; errorCard(T.err_generic, (r && r.error) || "Thumbnail generation failed."); }
    }, "secondary");
    thumbs.appendChild(sectionHead("Thumbnail + title", "Every generation creates three distinct image/title pairs.", gen));
    const list = el("div", "lf-thumbnail-options"); thumbs.appendChild(list);
    if (!(data.thumbnails || []).length) {
      list.appendChild(el("div", "lf-empty-state", "No thumbnail set yet. Generate three options before rendering.")); return;
    }
    data.thumbnails.forEach(t => {
      const card = el("article", "lf-thumbnail-card" + (t.selected ? " selected" : ""));
      const im = el("img"); im.src = t.img; im.alt = "Thumbnail option " + (t.index + 1); im.loading = "lazy"; card.appendChild(im);
      const body = el("div", "lf-thumbnail-copy"); card.appendChild(body);
      body.appendChild(el("span", "lf-option-label", t.selected ? "SELECTED" : "OPTION " + (t.index + 1)));
      body.appendChild(el("h4", "", esc(t.title || "Untitled concept")));
      if (!t.selected) body.appendChild(btn("Use thumbnail + title", async ev => {
        ev.currentTarget.disabled = true;
        const r = await jpost("/longform-thumbnail-select", { slug: data.slug, index: t.index });
        if (!r || !r.ok) errorCard(T.err_generic, (r && r.error) || "Selection failed."); await refresh();
      }, "ghost small"));
      list.appendChild(card);
    });
  }
  function paintTimeline() {
    timeline.innerHTML = "";
    const frames = data.frames || [];
    if (!frames.length) { timeline.appendChild(el("div", "lf-empty-state", "No timed images available.")); return; }
    if (!frames.some(f => f.idx === selectedFrame)) selectedFrame = frames[0].idx;
    const chosen = () => frames.find(f => f.idx === selectedFrame) || frames[0];
    const count = el("span", "lf-duration", `${frames.length} images / ${Number(data.audio_duration || 0).toFixed(1)}s`);
    timeline.appendChild(sectionHead("Timeline", "The same time scale controls both tracks; clip width now represents its real duration.", count));
    // Range retime is intentionally local: it only redistributes the selected narration
    // span and matches the images already in those slots. It never generates media.
    const retimeBar = el("div", "lf-retime-bar");
    retimeBar.appendChild(el("span", "lf-track-help", "Retime a scene range with existing images"));
    const from = document.createElement("input"); from.type = "number"; from.min = "1"; from.max = String(frames.length);
    from.value = String(Math.min(selectedFrame + 1, frames.length)); from.title = "First scene";
    const to = document.createElement("input"); to.type = "number"; to.min = "1"; to.max = String(frames.length);
    to.value = String(Math.min(selectedFrame + 2, frames.length)); to.title = "Last scene";
    const retimeBtn = btn("Analyze + retime", async ev => {
      const a = Math.max(1, Math.min(frames.length, Number(from.value || 1))) - 1;
      const b = Math.max(1, Math.min(frames.length, Number(to.value || frames.length))) - 1;
      if (b <= a) { errorCard(T.err_generic, "Choose at least two scenes."); return; }
      ev.currentTarget.disabled = true; ev.currentTarget.textContent = "Retiming…";
      const r = await jpost("/longform-retime", {slug: data.slug, start_idx: a, end_idx: b});
      if (!r || !r.ok) errorCard(T.err_generic, (r && r.error) || "Could not retime the selected range.");
      else await refresh();
      ev.currentTarget.disabled = false; ev.currentTarget.textContent = "Analyze + retime";
    }, "secondary small");
    retimeBar.appendChild(el("span", "", "Scenes"));
    retimeBar.appendChild(from); retimeBar.appendChild(el("span", "", "–")); retimeBar.appendChild(to);
    retimeBar.appendChild(retimeBtn); timeline.appendChild(retimeBar);

    const workspace = el("div", "lf-nle-workspace"); timeline.appendChild(workspace);
    const preview = el("div", "lf-nle-preview"); workspace.appendChild(preview);
    const previewImg = el("img"); previewImg.alt = "Selected timeline image"; preview.appendChild(previewImg);
    const previewEmpty = el("span", "lf-preview-empty", "MISSING IMAGE"); preview.appendChild(previewEmpty);
    const previewTime = el("span", "lf-preview-time"); preview.appendChild(previewTime);
    // The narration transport belongs to the picture it controls.  It used to sit in the
    // inspector, which made the visual preview look frozen and forced the user to hunt for
    // playback in a side panel.
    if (data.voiceover) { audio.src = data.voiceover; preview.appendChild(audio); }
    const inspector = el("div", "lf-nle-inspector"); workspace.appendChild(inspector);
    inspector.appendChild(el("div", "lf-eyebrow", "SELECTED IMAGE"));
    const selectedTitle = el("h4"); inspector.appendChild(selectedTitle);
    const selectedLine = el("p", "lf-selected-line"); inspector.appendChild(selectedLine);
    const selectedActions = el("div", "lf-selected-actions"); inspector.appendChild(selectedActions);
    const replaceBtn = btn("Replace image", () => { const f=chosen(); pickFile("image/*", async file => {
      const fd=new FormData(); fd.append("slug",data.slug); fd.append("idx",String(f.idx)); fd.append("file",file,file.name);
      const r=await (await fetch("/longform-frame-upload",{method:"POST",body:fd})).json();
      if(!r.ok) errorCard(T.err_generic,r.error||"Upload failed."); await refresh();
    }); }, "ghost small");
    const moveBtn = btn("Move / swap", () => { swapFrom=chosen().idx; armedUnused=null; paintTimeline(); }, "ghost small");
    selectedActions.appendChild(replaceBtn); selectedActions.appendChild(moveBtn);
    if (!data.voiceover) inspector.appendChild(el("div", "lf-empty-inline", "Voiceover unavailable"));

    const updatePreview = f => {
      if (!f) return;
      selectedFrame=f.idx; previewImg.hidden=!f.exists; previewEmpty.hidden=!!f.exists;
      if(f.exists) previewImg.src=f.img+(f.img.includes("?")?"&":"?")+"v="+Date.now();
      previewTime.textContent=`${f.ts} / ${Number(f.dur||0).toFixed(1)}s`;
      selectedTitle.textContent=`Image ${f.idx+1}`; selectedLine.textContent=f.text||"No voiceover line";
    };
    updatePreview(chosen());

    // This must be defined before the zoom control.  The old ordering referenced `duration`
    // while it was still in JavaScript's temporal-dead-zone, which aborted the entire editor
    // after painting the first frame.  That left the preview frozen and prevented the render
    // footer below from ever being created.
    const duration=Math.max(Number(data.audio_duration||0),Number(frames[frames.length-1].end||0),1);
    const tools = el("div", "lf-nle-toolbar"); timeline.appendChild(tools);
    tools.appendChild(el("span", "lf-track-help", swapFrom!==null ? "Choose another image slot to complete the swap." : armedUnused ? "Choose an image slot for the unused asset." : "Scroll / drag the timeline to move through the video - drag clips to swap, click to inspect"));
    const zoomLabel=el("label","lf-zoom-control","Zoom");
    const zoom=el("select"); [["fit","Fit whole video"],["4","Overview"],["14","Normal"],["28","Detailed"]].forEach(([v,n])=>zoom.appendChild(new Option(n,v)));
    zoom.value=String(zoomChoice||(Number(duration)<=240?"fit":"4")); zoom.addEventListener("change",()=>{zoomChoice=zoom.value;paintTimeline();});
    zoomLabel.appendChild(zoom); tools.appendChild(zoomLabel);

    const rows=el("div","lf-nle-rows"); timeline.appendChild(rows);
    const labels=el("div","lf-nle-labels","<span></span><b>Images</b><b>Voiceover</b>"); rows.appendChild(labels);
    const scroll=el("div","lf-nle-scroll"); rows.appendChild(scroll);
    // "fit" shows the ENTIRE video without scrolling; numeric presets are px per second.
    const availW=Math.max(320,(timeline.clientWidth||900)-120);
    // Anything that comfortably fits opens fitted; a long video opens at the overview scale,
    // where fitting would squeeze twenty minutes into an unclickable strip.
    if(zoomChoice===null) zoomChoice = duration<=240 ? "fit" : "4";
    timelineScale=zoomChoice==="fit"?Math.max(0.3,(availW-10)/duration):Number(zoomChoice);
    // Never wider than it needs to be: the 900px floor is what created the empty right-hand
    // two thirds on a short video.
    const width=zoomChoice==="fit"?availW-10
      :Math.max(Math.min(900,availW-10),Math.ceil(duration*timelineScale));
    const canvas=el("div","lf-nle-canvas"); canvas.style.width=width+"px"; scroll.appendChild(canvas);
    // Mouse navigation (user 2026-07-23: "ich kann nicht weiter ruebergehen"): a plain
    // wheel scrolls the timeline horizontally, and dragging the background pans it -
    // the thin scrollbar is no longer the only way to move through a 20-minute video.
    let suppressSeek=0;
    scroll.addEventListener("wheel",ev=>{ if(Math.abs(ev.deltaY)>Math.abs(ev.deltaX)){ ev.preventDefault(); scroll.scrollLeft+=ev.deltaY; } },{passive:false});
    let panStart=null;
    scroll.addEventListener("pointerdown",ev=>{ if(ev.button!==0||ev.target.closest(".lf-nle-clip"))return; panStart={x:ev.clientX,left:scroll.scrollLeft,moved:false}; });
    scroll.addEventListener("pointermove",ev=>{ if(!panStart)return; const dx=ev.clientX-panStart.x;
      if(!panStart.moved&&Math.abs(dx)>4){panStart.moved=true;scroll.classList.add("panning");}
      if(panStart.moved) scroll.scrollLeft=panStart.left-dx; });
    const endPan=()=>{ if(panStart&&panStart.moved) suppressSeek=Date.now(); panStart=null; scroll.classList.remove("panning"); };
    scroll.addEventListener("pointerup",endPan); scroll.addEventListener("pointerleave",endPan);
    const ruler=el("div","lf-nle-ruler"); canvas.appendChild(ruler);
    const imageTrack=el("div","lf-nle-track lf-nle-image-track"); canvas.appendChild(imageTrack);
    const voiceTrack=el("div","lf-nle-track lf-nle-voice-track"); canvas.appendChild(voiceTrack);
    const playhead=el("div","lf-nle-playhead"); canvas.appendChild(playhead);
    const tickStep=timelineScale<=14?60:timelineScale<=28?30:10;
    for(let t=0;t<=duration;t+=tickStep){const tick=el("span","lf-nle-tick",`${Math.floor(t/60)}:${String(Math.floor(t%60)).padStart(2,"0")}`);tick.style.left=(t*timelineScale)+"px";ruler.appendChild(tick);}
    const seekAt=ev=>{if(Date.now()-suppressSeek<250)return;const rect=canvas.getBoundingClientRect();const t=Math.max(0,Math.min(duration,(ev.clientX-rect.left)/timelineScale));if(data.voiceover)audio.currentTime=t;playhead.style.left=(t*timelineScale)+"px";const f=frames.find(x=>t>=Number(x.start)&&t<Number(x.end));if(f){imageTrack.querySelectorAll(".selected").forEach(x=>x.classList.remove("selected"));const node=imageTrack.querySelector(`[data-idx="${f.idx}"]`);if(node)node.classList.add("selected");updatePreview(f);}};
    ruler.addEventListener("click",seekAt); imageTrack.addEventListener("click",ev=>{if(ev.target===imageTrack)seekAt(ev);}); voiceTrack.addEventListener("click",seekAt);
    audio.ontimeupdate=()=>{const t=Number(audio.currentTime||0);playhead.style.left=(t*timelineScale)+"px";const f=frames.find(x=>t>=Number(x.start)&&t<Number(x.end));if(f&&f.idx!==selectedFrame)updatePreview(f);};

    // The opener occupies real screen time before frame 1. Showing it here is what makes the
    // timeline match the finished video instead of starting mid-way through it.
    if (data.opener_enabled) {
      const secs = Number(data.opener_seconds || data.opener_expected_seconds || 4);
      const head = el("div", "lf-nle-clip lf-nle-opener" + (data.opener_clip ? " filled" : " missing"));
      head.style.left = "0px";
      head.style.width = Math.max(10, secs * timelineScale) + "px";
      head.innerHTML = "<span>" + (data.opener_clip ? "OPENER" : "OPENER?") + "</span><em>" + secs.toFixed(1) + "s</em>";
      head.title = data.opener_clip ? "Your generated opening clip" : "Placeholder - the built-in clip is used until you insert yours";
      head.addEventListener("click", ev => { ev.stopPropagation(); opener.scrollIntoView({behavior: "smooth", block: "center"}); });
      imageTrack.appendChild(head);
    }
    frames.forEach(f => {
      const tile = el("div", "lf-nle-clip" + (f.exists ? "" : " missing") + (selectedFrame===f.idx?" selected":"") + (swapFrom===f.idx?" swap-src":""));
      tile.dataset.idx=String(f.idx); tile.style.left=(Number(f.start||0)*timelineScale)+"px";
      tile.style.width=Math.max(10,Number(f.dur||0)*timelineScale)+"px";
      if(f.exists) tile.style.backgroundImage=`url("${f.img}")`;
      tile.innerHTML=`<span>${f.idx+1}</span><em>${Number(f.dur||0).toFixed(1)}s</em>`;
      tile.draggable = !!f.exists;
      tile.addEventListener("dragstart", ev => { ev.dataTransfer.setData("application/json", JSON.stringify({kind:"frame",idx:f.idx})); tile.classList.add("dragging"); });
      tile.addEventListener("dragend", () => tile.classList.remove("dragging"));
      tile.addEventListener("dragover", ev => { ev.preventDefault(); tile.classList.add("drop-target"); });
      tile.addEventListener("dragleave", () => tile.classList.remove("drop-target"));
      tile.addEventListener("drop", ev => { ev.preventDefault(); tile.classList.remove("drop-target"); try { place(JSON.parse(ev.dataTransfer.getData("application/json")), f.idx); } catch (e) {} });
      tile.addEventListener("click", ev => {
        ev.stopPropagation();
        if (armedUnused) return place({kind:"unused",rel_path:armedUnused}, f.idx);
        if (swapFrom !== null && swapFrom !== f.idx) return place({kind:"frame",idx:swapFrom}, f.idx);
        selectedFrame=f.idx; imageTrack.querySelectorAll(".selected").forEach(x=>x.classList.remove("selected"));tile.classList.add("selected");updatePreview(f);
      });
      imageTrack.appendChild(tile);
      const vo=el("div","lf-nle-voice-segment"); vo.style.left=(Number(f.start||0)*timelineScale)+"px";
      vo.style.width=Math.max(10,Number(f.dur||0)*timelineScale)+"px"; vo.title=f.text||"";
      vo.innerHTML=`<span>${esc(f.text||"")}</span>`; voiceTrack.appendChild(vo);
    });
  }
  function paintUnused() {
    unused.innerHTML = "";
    unused.appendChild(sectionHead("Unused generated images", "Archived images show where they were originally intended to appear.", el("span", "lf-count", String((data.unused || []).length))));
    const tray = el("div", "lf-unused-tray"); unused.appendChild(tray);
    if (!(data.unused || []).length) { tray.appendChild(el("div", "lf-empty-state", "No unused generated images in this project.")); return; }
    data.unused.forEach(u => {
      const item = el("article", "lf-unused-item" + (armedUnused === u.rel_path ? " armed" : "")); item.draggable = true;
      item.addEventListener("dragstart", ev => ev.dataTransfer.setData("application/json", JSON.stringify({kind:"unused",rel_path:u.rel_path})));
      const im = el("img"); im.src = u.img; im.alt = "Unused generated image"; im.loading = "lazy"; im.draggable = false; item.appendChild(im);
      const cp = el("div", "lf-unused-copy"); item.appendChild(cp);
      cp.appendChild(el("b", "", `Originally for #${u.intended_idx + 1} / ${esc(u.intended_ts)}`));
      cp.appendChild(el("p", "", esc(u.intended_text || ""))); cp.appendChild(el("small", "", esc(u.folder || "Archive")));
      cp.appendChild(btn(armedUnused === u.rel_path ? "Choose a slot above" : "Place on timeline", () => {
        armedUnused = armedUnused === u.rel_path ? null : u.rel_path; swapFrom = null; paintUnused(); paintTimeline();
      }, "ghost tiny")); tray.appendChild(item);
    });
  }
  function paintAll() { paintOpener(); paintThumbs(); paintTimeline(); paintUnused(); }
  paintAll();
  const foot = el("div", "card-foot lf-render-foot");
  foot.appendChild(el("div", "lf-render-copy", "<b>Ready to render?</b><span>Your existing images and selected thumbnail are saved before assembly.</span>"));
  foot.appendChild(el("span", "spacer"));
  foot.appendChild(btn("Render video", async ev => {
    ev.currentTarget.disabled = true;
    const r = await jpost("/longform-rebuild", {slug:data.slug});
    if (r && r.ok && r.id) { closeEditor(); startJob(r.id); }
    else { ev.currentTarget.disabled = false; errorCard(T.err_generic, (r && r.error) || "Render failed."); }
  }, "primary"));
  root.appendChild(foot);
}

/* ---------------------------------------------------- legacy longform frame editor fallback */
async function openLongformFrameEditor(slug) {
  return openLongformPreRenderEditor(slug);
  /* istanbul ignore next -- retained only as an emergency reference for old saved builds */
  document.querySelectorAll(".lf-frame-editor").forEach(x => x.remove());
  let d;
  try { d = await jget("/longform-frames?slug=" + encodeURIComponent(slug)); } catch (e) { d = null; }
  if (!d || !d.ok) { errorCard(T.err_generic, (d && d.error) || "Could not load the frames."); return; }
  const c = el("div", "chat-card lf-frame-editor"); chat.appendChild(c);
  c.appendChild(el("h3", "", "🖼 Frame editor — " + esc(d.slug)));
  c.appendChild(el("div", "card-note",
    "Click a frame to replace or move it. Missing frames are black in the video. " +
    "Rebuild renders a new MP4 from the frames below (the previous render is kept)."));
  const grid = el("div", "lf-frames"); c.appendChild(grid);
  let swapFrom = null;                       // idx armed for "Move/Swap" (click a target next)
  const bust = () => "&t=" + Date.now();

  const paint = (frames) => {
    grid.innerHTML = "";
    frames.forEach(f => {
      const tile = el("div", "lf-frame" + (f.exists ? "" : " missing") +
                              (swapFrom === f.idx ? " swap-src" : ""));
      tile.dataset.idx = f.idx;
      const im = el("div", "lf-frame-img");
      if (f.exists) im.style.backgroundImage = `url("${f.img}${f.img.includes("?") ? bust() : ""}")`;
      else im.textContent = "BLACK";
      tile.appendChild(im);
      tile.appendChild(el("div", "lf-frame-meta",
        `<b>#${f.idx + 1}</b> ${esc(f.ts)} · ${(+f.dur).toFixed(1)}s`));
      tile.appendChild(el("div", "lf-frame-text", esc((f.text || "").slice(0, 60))));
      tile.title = f.text || "";
      tile.addEventListener("click", async () => {
        if (swapFrom !== null && swapFrom !== f.idx) {
          const r = await jpost("/longform-frame-swap", { slug: d.slug, a: swapFrom, b: f.idx });
          swapFrom = null;
          if (!r || !r.ok) errorCard(T.err_generic, (r && r.error) || "Swap failed.");
          refresh();
          return;
        }
        // per-frame action menu (small, attached to the tile)
        const old = tile.querySelector(".lf-frame-menu");
        if (old) { old.remove(); return; }
        grid.querySelectorAll(".lf-frame-menu").forEach(x => x.remove());
        const menu = el("div", "lf-frame-menu");
        menu.appendChild(btn("⬆ Replace…", (ev) => {
          ev.stopPropagation();
          pickFile("image/*", async file => {
            const fd = new FormData();
            fd.append("slug", d.slug); fd.append("idx", String(f.idx));
            fd.append("file", file, file.name);
            const r = await (await fetch("/longform-frame-upload", { method: "POST", body: fd })).json();
            if (!r.ok) errorCard(T.err_generic, r.error || "Upload failed.");
            refresh();
          });
        }, "small"));
        menu.appendChild(btn("⇄ Move/Swap…", (ev) => {
          ev.stopPropagation();
          swapFrom = f.idx; paint(lastFrames);
        }, "ghost small"));
        menu.addEventListener("click", ev => ev.stopPropagation());
        tile.appendChild(menu);
      });
      grid.appendChild(tile);
    });
  };

  let lastFrames = d.frames;
  const refresh = async () => {
    try {
      const nd = await jget("/longform-frames?slug=" + encodeURIComponent(d.slug));
      if (nd && nd.ok) { lastFrames = nd.frames; paint(lastFrames); }
    } catch (e) {}
  };
  paint(lastFrames);

  const foot = el("div", "card-foot");
  foot.appendChild(el("span", "spacer"));
  foot.appendChild(btn("✖ Close", () => c.remove(), "ghost"));
  // Deliberately AFTER the video exists and behind its own button: seven translations plus
  // seven full voiceovers is the most expensive thing here, so it waits until you have watched
  // this cut and decided to keep it.
  foot.appendChild(btn("🌍 Generate language tracks", async (ev) => {
    if (!confirm("Translate and speak this video in 7 languages (Spanish, Portuguese, Korean, "
      + "Indonesian, Japanese, German, French)?\n\nThat is 7 translations and 7 full voiceovers "
      + "- the most expensive job here. The .mp3 and .srt of each land in .renders.")) return;
    ev.currentTarget.disabled = true;
    const r = await jpost("/longform-languages", { slug: d.slug });
    if (r && r.ok && r.id) { c.remove(); startJob(r.id); }
    else { ev.currentTarget.disabled = false; errorCard(T.err_generic, (r && r.error) || "Could not start."); }
  }, "ghost"));
  foot.appendChild(btn("🎬 Rebuild video", async (ev) => {
    ev.currentTarget.disabled = true;
    const r = await jpost("/longform-rebuild", { slug: d.slug });
    if (r && r.ok && r.id) { c.remove(); startJob(r.id); }
    else { ev.currentTarget.disabled = false; errorCard(T.err_generic, (r && r.error) || "Rebuild failed."); }
  }, "primary"));
  c.appendChild(foot);
  scrollDown();
}
async function cancelJob() {
  if (!S.jobId) return;
  await fetch("/cancel?id=" + encodeURIComponent(S.jobId), { method: "POST" });
  S.jobStatus = "cancelling"; renderTopbar(); persist();
}

/* ------------------------------------------------------------------ presets */
async function openPresets() {
  const data = await jget("/presets");
  const scrim = el("div", "modal-scrim"); document.body.appendChild(scrim);
  const close = () => scrim.remove();
  scrim.addEventListener("click", e => { if (e.target === scrim) close(); });
  document.addEventListener("keydown", function esc_(e) { if (e.key === "Escape") { close(); document.removeEventListener("keydown", esc_); } });
  const m = el("div", "modal"); scrim.appendChild(m);
  m.appendChild(el("h3", "", esc(T.presets)));
  const list = el("div", "list"); m.appendChild(list);
  const applyPreset = (name, obj) => {
    Object.keys(obj || {}).forEach(k => {
      if (MAN.run.text.includes(k)) S.values[k] = obj[k] != null ? String(obj[k]) : "";
      else if (MAN.run.check.includes(k) || MAN.run.state_hidden.includes(k)) S.values[k] = !!obj[k];
    });
    close();
    const note = card(); note.appendChild(el("div", "card-note", "✓ " + esc(T.preset_applied) + ": " + esc(name)));
    renderAll(); persist();
  };
  Object.entries(data.builtin || {}).forEach(([name, obj]) => {
    const row = el("div", "chip-add");
    row.appendChild(btn(name, () => applyPreset(name, obj)));
    list.appendChild(row);
  });
  Object.entries(data.user || {}).forEach(([name, obj]) => {
    const row = el("div", "chip-add");
    row.appendChild(btn(name, () => applyPreset(name, obj)));
    row.appendChild(btn(T.overwrite, async () => { await savePreset(name); close(); }, "ghost small"));
    row.appendChild(btn(T.delete_preset, async () => {
      await jpost("/delete-preset", { name }); close(); openPresets();
    }, "danger small"));
    list.appendChild(row);
  });
  const saveRow = el("div", "chip-add");
  const inp = el("input"); inp.type = "text"; inp.placeholder = T.save_as;
  saveRow.appendChild(inp);
  saveRow.appendChild(btn("💾 " + T.save_preset, async () => {
    const name = inp.value.trim(); if (!name) { inp.focus(); return; }
    await savePreset(name); close();
  }, "primary"));
  m.appendChild(saveRow);
}
async function savePreset(name) {
  const fields = OPT.preset_fields && OPT.preset_fields.length ? OPT.preset_fields
    : MAN.run.text.concat(MAN.run.check);
  const data = {};
  fields.forEach(k => { if (S.values[k] !== undefined) data[k] = S.values[k]; });
  await jpost("/save-preset", { name, data });
}

/* ------------------------------------------------------------------ project picker + assets view */
async function openProjectPicker() {
  const d = await jget("/projects-list");
  const scrim = el("div", "modal-scrim"); document.body.appendChild(scrim);
  const close = () => scrim.remove();
  scrim.addEventListener("click", e => { if (e.target === scrim) close(); });
  document.addEventListener("keydown", function esc_(e) { if (e.key === "Escape") { close(); document.removeEventListener("keydown", esc_); } });
  const m = el("div", "modal"); scrim.appendChild(m);
  m.appendChild(el("h3", "", esc(T.pick_project)));
  const list = el("div", "list"); m.appendChild(list);
  (d.projects || []).forEach(p => {
    const b = el("button", "sb-proj");
    // state chip only when it says something ("ok" on every healthy row was noise)
    const st = projState(p);
    b.innerHTML = `${projThumb(p, "th")}
      <span class="meta"><b>${esc(p.title)}</b><span>${esc(fmtDate(p.edited))}</span></span>
      ${st ? `<span class="st ${st === "failed" ? "fail" : st === "awaiting" ? "wait" : "ok"}">${STATE_LABEL[st]}</span>` : ""}`;
    b.addEventListener("click", () => { close(); loadProject(p.slug, p); });
    list.appendChild(b);
  });
  if (!(d.projects || []).length) m.appendChild(el("div", "card-note", esc(T.no_projects)));
}

function showAssets(showHidden) {
  showLoading(T.nav_assets + "...");
  S.view = "assets"; S.showHidden = !!showHidden; renderAll(); persist();
}
let assetsProjects = [];
// One-shot search prefill from /assets?q=... - deliberately NOT in S, so it does not persist
// into the next visit to the library.
let assetsInitialQuery = "";
async function renderAssetsView() {
  const head = el("div", "assets-head");
  head.appendChild(el("h2", "", esc(T.nav_assets)));
  // Search lives here now (moved out of the sidebar): filters this project & asset library.
  const search = el("input", "assets-search");
  search.type = "search"; search.placeholder = T.search_projects;
  search.setAttribute("aria-label", T.search_projects);
  head.appendChild(search);
  // The way back from the Library leads into the showroom, never into the old launchpad. Only
  // when this document was deliberately opened as the classic console (?ui=chat) does the
  // button stay inside it - that query is the one escape hatch, and it names itself.
  const back = btn(isClassicConsole() ? (prototypeMode ? "Back to launchpad" : ("← " + T.new_chat)) : "Showroom",
    () => { if (!isClassicConsole()) { location.href = "/"; return; } S.view = "chat"; renderAll(); persist(); }, "ghost small");
  head.appendChild(back);
  chat.appendChild(head);
  const grid = el("div", "assets-grid"); chat.appendChild(grid);
  const empty = el("div", "card-note", esc(T.no_projects)); empty.hidden = true; chat.appendChild(empty);
  const footer = el("div", "card-foot"); chat.appendChild(footer);
  const d = await jget("/projects-list" + (S.showHidden ? "?hidden=1" : ""));
  hideLoading();
  assetsProjects = d.projects || [];
  let visibleCount = prototypeMode ? 24 : Number.POSITIVE_INFINITY;
  const loadMore = btn("Load more", () => { visibleCount += 24; paint(); }, "ghost");
  footer.appendChild(loadMore);
  const paint = () => {
    const q = (search.value || "").trim().toLowerCase();
    grid.innerHTML = "";
    const rows = assetsProjects.filter(p => !q ||
      (p.title || "").toLowerCase().includes(q) || (p.slug || "").toLowerCase().includes(q));
    rows.slice(0, visibleCount).forEach(p => grid.appendChild(assetCard(p)));
    loadMore.hidden = rows.length <= visibleCount;
    empty.hidden = rows.length > 0;
  };
  search.addEventListener("input", () => {
    visibleCount = prototypeMode ? 24 : Number.POSITIVE_INFINITY;
    paint();
  });
  if (assetsInitialQuery) { search.value = assetsInitialQuery; assetsInitialQuery = ""; }
  paint();
  if (S.showHidden) footer.appendChild(btn("← " + T.hide_hidden, () => showAssets(false), "ghost"));
  else if (d.hidden_count) footer.appendChild(btn(`👁 ${T.show_hidden} (${d.hidden_count})`, () => showAssets(true), "ghost"));
  setTimeout(() => { try { search.focus(); } catch (e) {} }, 30);
}
function assetCard(p) {
  const c = el("div", "asset-card2");
  const previewKind=p.running?'running':p.failed?'failed':(p.kind||p.preview_kind||'');
  if(previewKind)c.classList.add('project-kind-'+previewKind);
  const hideB = el("button", "ahide", S.showHidden ? "↺" : "✕");
  hideB.title = S.showHidden ? T.unhide : T.hide;
  hideB.setAttribute("aria-label", hideB.title);
  hideB.addEventListener("click", async () => {
    await jpost("/hide-project", { slug: p.slug, hidden: !S.showHidden });
    c.remove(); refreshSidebar();
  });
  c.appendChild(hideB);
  const fig = el("div", "afig"); fig.innerHTML = projThumb(p, "ath"); c.appendChild(fig);
  const body = el("div", "abody");
  const tt = el("b", "", esc(p.title));
  const ren = el("button", "", "✎"); ren.title = T.rename; ren.setAttribute("aria-label", T.rename);
  ren.addEventListener("click", () => renameProject(p.slug, p.title, tt));
  tt.appendChild(ren); body.appendChild(tt);
  body.appendChild(el("span", "", "Edited " + esc(fmtDate(p.edited))));
  const pills = el("div", "apills");
  pills.innerHTML = `<span class="apill">${p.counters.web} web</span><span class="apill">${p.counters.gpt} GPT</span><span class="apill">${p.counters.clips} clips</span>`;
  body.appendChild(pills);
  c.appendChild(body);
  const act = el("div", "aact");
  if (p.awaiting) act.appendChild(btn("▶ " + T.continue, () => loadProject(p.slug, p), "primary small"));
  else if (p.failed) act.appendChild(btn("▶ " + T.continue, () => continueProject(p.slug), "primary small"));
  else if (p.results_url) act.appendChild(linkBtn("▶ " + T.check_results, p.results_url, "small"));
  // Opening a finished project goes STRAIGHT into the timeline editor (its render exists);
  // only projects without a render fall back to the project overview.
  if (p.has_timeline) act.appendChild(btn("🎞 " + (T.open || "Open"), () => openTimelineWithLoading(p.slug, p.title), "small"));
  else act.appendChild(btn("↺ " + (T.open || "Open"), () => loadProject(p.slug, p), "small"));
  c.appendChild(act);
  return c;
}
async function renameProject(slug, current, node) {
  const next = prompt(T.rename + ":", current || "");
  if (next == null || !next.trim() || next === current) return;
  const d = await jpost("/rename-project", { slug, title: next.trim() });
  if (d && d.ok) {
    if (node) node.firstChild ? node.firstChild.textContent = d.title || next : node.textContent = d.title || next;
    if (S.projectSlug === slug) { S.projectTitle = d.title || next; renderTopbar(); }
    refreshSidebar();
  } else alert((d && d.error) || T.err_generic);
}

/* ------------------------------------------------------------------ sidebar */
const NAV = [
  ["new", "＋", T.nav_new, resetToMode],
  ["assets", "▦", T.nav_assets, () => showAssets(false)],
  ["sfx", "🔊", T.nav_sfx, () => { resetToMode(); selectMode("sfx"); }],
  ["vfx", "➜", T.nav_vfx, () => { resetToMode(); selectMode("visual"); }],
  ["captions", "💬", T.nav_captions, () => { resetToMode(); selectMode("captions"); }],
];
function renderNav() {
  const nav = $("sb-nav"); nav.innerHTML = "";
  NAV.filter(([id]) => ["new", "assets"].includes(id)).forEach(([id, ico, label, fn]) => {
    const b = el("button");
    const protoNavIcons = { new: "spark", assets: "folder", sfx: "sound", vfx: "visual", captions: "captions" };
    b.innerHTML = prototypeMode
      ? `<span class="ico">${protoIcon(protoNavIcons[id] || "spark")}</span><span>${esc(label)}</span>`
      : `<span class="ico">${ico}</span><span>${esc(label)}</span>`;
    b.dataset.nav = id;
    b.addEventListener("click", () => { fn(); markNav(id); closeDrawer(); });
    nav.appendChild(b);
  });
  markNav(S.view === "assets" ? "assets" : (S.flow ? { sfx: "sfx", visual: "vfx", captions: "captions" }[S.flow] || "new" : "new"));
}
function markNav(id) {
  document.querySelectorAll("#sb-nav button").forEach(b => b.classList.toggle("active", b.dataset.nav === id));
}
async function openTimelineNav() {
  if (S.projectSlug) {
    const d = await jget("/projects-list");
    const p = (d.projects || []).find(x => x.slug === S.projectSlug);
    if (p && p.has_timeline) {
      openTimelineWithLoading(S.projectSlug, p.title || S.projectTitle); return;
    }
    alert(T.no_timeline_yet); return;
  }
  // no active project: a chat-shell modal (open an existing timeline OR upload your own video),
  // rendered in the current design instead of an old server page.
  const d = await jget("/projects-list");
  const withTl = (d.projects || []).filter(p => p.has_timeline);
  const scrim = el("div", "modal-scrim"); document.body.appendChild(scrim);
  scrim.addEventListener("click", e => { if (e.target === scrim) scrim.remove(); });
  const m = el("div", "modal tl-open-modal"); scrim.appendChild(m);
  m.appendChild(el("h3", "", esc(T.open_timeline || "Timeline Editor")));
  const sub = el("p", "tl-open-sub", esc(T.timeline_open_sub ||
    "Open a rendered project to fine-tune it, or drop in your own video to start editing from scratch."));
  m.appendChild(sub);

  // upload-your-own-video row
  const up = el("div", "tl-open-upload");
  const upBtn = el("button", "btn primary", "⬆ " + (T.upload_own_video || "Load your own video"));
  const file = el("input"); file.type = "file"; file.accept = "video/mp4,.mp4"; file.hidden = true;
  const status = el("div", "tl-open-status"); status.hidden = true;
  up.appendChild(upBtn); up.appendChild(file); m.appendChild(up); m.appendChild(status);
  upBtn.addEventListener("click", () => file.click());
  file.addEventListener("change", async () => {
    const f = file.files && file.files[0]; if (!f) return;
    if (!/\.mp4$/i.test(f.name) && f.type !== "video/mp4") {
      status.hidden = false; status.className = "tl-open-status error"; status.textContent = T.upload_mp4_only || "Please choose an .mp4 file."; return;
    }
    status.hidden = false; status.className = "tl-open-status"; status.textContent = (T.uploading || "Uploading") + " " + f.name + " …";
    upBtn.disabled = true;
    try {
      const fd = new FormData(); fd.append("file", f, f.name);
      const r = await fetch("/timeline-import", { method: "POST", body: fd });
      const j = await r.json();
      if (j && j.ok && j.slug) { scrim.remove(); openTimelineWithLoading(j.slug, f.name.replace(/\.mp4$/i, "")); }
      else { status.className = "tl-open-status error"; status.textContent = (j && j.error) || (T.upload_failed || "Upload failed."); upBtn.disabled = false; }
    } catch (e) { status.className = "tl-open-status error"; status.textContent = T.upload_failed || "Upload failed."; upBtn.disabled = false; }
  });

  // existing timeline-capable projects
  if (withTl.length) {
    m.appendChild(el("div", "tl-open-divider", esc(T.or_open_project || "or open a project")));
    const list = el("div", "list"); m.appendChild(list);
    withTl.forEach(p => {
      const b = el("button", "sb-proj");
      b.innerHTML = `${projThumb(p, "th")}<span class="meta"><b>${esc(p.title)}</b><span>${esc(fmtDate(p.edited))}</span></span>`;
      b.addEventListener("click", () => { scrim.remove(); openTimelineWithLoading(p.slug, p.title); });
      list.appendChild(b);
    });
  }
}
let sidebarProjects = [];
async function refreshSidebar() {
  try {
    const d = await jget("/projects-list");
    sidebarProjects = d.projects || [];
    paintSidebarProjects();
  } catch (e) {}
  try {
    const jd = await jget("/jobs-list");
    const wrap = $("sb-jobs-wrap"); const box = $("sb-jobs");
    const active = (jd.jobs || []).filter(j => ["running", "cancelling", "awaiting_approval"].includes(j.status));
    wrap.hidden = !active.length;
    box.innerHTML = "";
    active.forEach(j => {
      const st = j.status;
      const label = st === "awaiting_approval" ? "Needs approval"
        : st === "cancelling" ? "Cancelling…" : "Working…";
      const title = prettyJobTitle(j.project_slug || j.kind || "Run");
      const b = el("button", "sb-job sb-job-" + (st === "awaiting_approval" ? "wait" : st === "cancelling" ? "cancel" : "run"));
      b.innerHTML =
        `<span class="sb-job-dot"></span>` +
        `<span class="sb-job-body"><b>${esc(title)}</b><em>${esc(label)}</em></span>` +
        `<span class="sb-job-wave"><i></i><i></i><i></i></span>`;
      b.title = title + " — " + (j.last_log || label);
      b.addEventListener("click", () => { startJob(j.id, j.kind); closeDrawer(); });
      box.appendChild(b);
    });
  } catch (e) {
    const wrap = $("sb-jobs-wrap");
    if (wrap) wrap.hidden = true;
  }
  paintConnections();
  refreshPrototypeHomeData();
}
function sidebarPinnedProjects() {
  try { return new Set(JSON.parse(localStorage.getItem("sl-pinned-projects") || "[]")); }
  catch (e) { return new Set(); }
}
function saveSidebarPins(pins) {
  try { localStorage.setItem("sl-pinned-projects", JSON.stringify([...pins])); } catch (e) {}
}
function openProjectContextMenu(event, project) {
  event.preventDefault(); event.stopPropagation();
  document.querySelector(".sb-project-menu")?.remove();
  const pins = sidebarPinnedProjects();
  const menu = el("div", "sb-project-menu");
  const action = (label, fn, danger) => {
    const item = el("button", danger ? "danger" : "", esc(label));
    item.addEventListener("click", async () => { menu.remove(); await fn(); });
    menu.appendChild(item);
  };
  action(pins.has(project.slug) ? "Unpin" : "Pin", () => {
    if (pins.has(project.slug)) pins.delete(project.slug); else pins.add(project.slug);
    saveSidebarPins(pins); paintSidebarProjects();
  });
  action("Rename", async () => {
    const next = prompt("New title:", project.title);
    if (!next || !next.trim() || next.trim() === project.title) return;
    const result = await jpost("/rename-project", { slug:project.slug, title:next.trim() });
    if (result && result.ok) project.title = result.title || next.trim();
    paintSidebarProjects();
  });
  action("Hide from list", async () => {
    await jpost("/hide-project", { slug:project.slug, hidden:true });
    sidebarProjects = sidebarProjects.filter(item => item.slug !== project.slug);
    pins.delete(project.slug); saveSidebarPins(pins); paintSidebarProjects();
  }, true);
  document.body.appendChild(menu);
  const left = Math.min(event.clientX, window.innerWidth - 170);
  const top = Math.min(event.clientY, window.innerHeight - 130);
  menu.style.left = Math.max(8, left) + "px"; menu.style.top = Math.max(8, top) + "px";
  const close = e => { if (!menu.contains(e.target)) { menu.remove(); document.removeEventListener("pointerdown", close); } };
  setTimeout(() => document.addEventListener("pointerdown", close), 0);
  menu.querySelector("button")?.focus();
}
function paintSidebarProjects() {
  const box = $("sb-projects");
  // Search moved to the Projects & Assets tab; the sidebar just lists the recent projects.
  box.innerHTML = "";
  const pins = sidebarPinnedProjects();
  sidebarProjects
    .slice()
    .sort((a, b) => Number(pins.has(b.slug)) - Number(pins.has(a.slug)))
    .slice(0, 24)
    .forEach(p => {
      const row = el("div", "sb-proj-row");
      const b = el("button", "sb-proj");
      b.innerHTML = `${projThumb(p, "th")}
        <span class="meta"><b>${esc(p.title)}</b><span>${esc(fmtDate(p.edited))}</span></span>
        ${p.awaiting ? '<span class="st wait" title="Waiting for your generated clips">○</span>'
          : p.failed ? '<span class="st fail" title="Failed - open to see what went wrong">!</span>' : ""}`;
      b.title = p.title;
      row.classList.toggle("pinned", pins.has(p.slug));
      row.addEventListener("contextmenu", e => openProjectContextMenu(e, p));
      // finished project (has a render) -> jump straight into the timeline editor; everything
      // the old project card offered lives there. Unfinished projects keep the chat flow.
      b.addEventListener("click", () => {
        if (p.has_timeline) { openTimelineWithLoading(p.slug, p.title); return; }
        loadProject(p.slug, p); closeDrawer();
      });
      row.appendChild(b); box.appendChild(row);
    });
}
const CONNS = [
  ["tiktok", "TikTok", "/tiktok-status", "/tiktok-login"],
  ["instagram", "Instagram", "/instagram-status", "/instagram-login"],
  ["higgsfield", "Higgsfield", "/higgsfield-status", "/higgsfield-login"],
];
async function paintConnections() {
  const box = $("sb-conns"); box.innerHTML = "";
  let ready = 0;
  for (const [key, label, statusUrl, loginUrl] of CONNS) {
    let st = (BOOT.connections || {})[key] || {};
    try { st = await jget(statusUrl); } catch (e) {}
    if (st.ready) ready++;
    const row = el("div", "sb-conn");
    const dot = el("span", "dot" + (st.busy ? " busy" : st.ready ? " on" : ""));
    row.appendChild(dot);
    row.appendChild(el("span", "", esc(label) + " · " + (st.busy ? T.busy : st.ready ? T.connected : T.not_connected)));
    const b = el("button", "", st.ready ? T.reconnect : "🔗");
    b.setAttribute("aria-label", (st.ready ? T.reconnect : "Connect") + " " + label);
    b.addEventListener("click", async () => { b.disabled = true; await jpost(loginUrl, {}); setTimeout(paintConnections, 1500); });
    row.appendChild(b);
    box.appendChild(row);
  }
  // Collapsed header shows just the count (e.g. "1/3"); the rows expand on hover/focus.
  const cnt = $("sb-conns-count"); if (cnt) cnt.textContent = ready + "/" + CONNS.length;
}
// platform key as stored in S.values.scrape_platforms (the backend splits this comma list)
function platformSelected(key) {
  return (S.values.scrape_platforms || "tiktok,x,instagram").split(",").map(s => s.trim()).includes(key);
}
function setPlatformSelected(key, on) {
  const cur = (S.values.scrape_platforms || "tiktok,x,instagram").split(",").map(s => s.trim()).filter(Boolean);
  const next = on ? Array.from(new Set(cur.concat([key]))) : cur.filter(p => p !== key);
  // keep a stable order and never let the list go fully empty (a scrape needs >=1 source)
  const order = ["tiktok", "x", "instagram"];
  S.values.scrape_platforms = (next.length ? next : ["tiktok"]).sort((a, b) => order.indexOf(a) - order.indexOf(b)).join(",");
}
function connectionRow(key, label, statusUrl, loginUrl, selectable) {
  const row = el("div", "sb-conn");
  const dot = el("span", "dot"); row.appendChild(dot);
  const txt = el("span", "conn-name", esc(label)); row.appendChild(txt);
  // the actions group (slider + reconnect) is pushed to the right as one unit, so the
  // "search this platform" slider sits directly next to the Reconnect button.
  const actions = el("div", "conn-actions"); row.appendChild(actions);
  if (selectable) {
    const sw = el("label", "conn-use");
    sw.title = "Use this platform when searching for clips";
    const cb = el("input"); cb.type = "checkbox"; cb.checked = platformSelected(key);
    const track = el("span", "conn-use-track");
    cb.addEventListener("change", () => { setPlatformSelected(key, cb.checked); persist(); });
    sw.appendChild(cb); sw.appendChild(track); sw.appendChild(el("span", "conn-use-lbl", "search"));
    actions.appendChild(sw);
  }
  const b = el("button", "", "🔗"); actions.appendChild(b);
  const paint = async () => {
    try {
      const st = await jget(statusUrl);
      dot.className = "dot" + (st.busy ? " busy" : st.ready ? " on" : "");
      txt.textContent = label.replace(/^Connect /, "") + " · " + (st.busy ? T.busy : st.ready ? T.connected : T.not_connected);
      b.textContent = st.ready ? T.reconnect : "🔗 Connect";
    } catch (e) {}
  };
  b.addEventListener("click", async () => { b.disabled = true; await jpost(loginUrl, {}); b.disabled = false; setTimeout(paint, 1500); });
  paint();
  return row;
}

/* ------------------------------------------------------------------ composer (removed)
   The bottom chatbox was dropped everywhere - it had no utility. Free-text steps (e.g. the
   viral topic) now render an inline text-input card in the chat; file-upload steps already
   use their own inline drop-zone. setComposer/wireComposer are kept as no-ops so the many
   setComposer("off") call sites stay harmless. */
function setComposer() {}
function wireComposer() {}
// Inline text-input card: a textarea + submit button rendered directly in the chat flow.
// Replaces the old composer "text" mode. Enter submits (Shift+Enter = newline).
function textInputCard(placeholder, onSubmit, initial) {
  const c = card("input-card");
  const ta = el("textarea", "input-card-ta");
  ta.rows = 2; ta.placeholder = placeholder || T.type_message;
  if (initial) ta.value = initial;
  const grow = () => { ta.style.height = "auto"; ta.style.height = Math.min(ta.scrollHeight, 200) + "px"; };
  ta.addEventListener("input", grow);
  const foot = el("div", "card-foot");
  const send = btn(T.send || "Send", () => {
    const v = ta.value.trim(); if (!v) return;
    onSubmit(v);
  }, "primary");
  ta.addEventListener("keydown", e => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send.click(); }
  });
  foot.appendChild(el("span", "spacer")); foot.appendChild(send);
  c.appendChild(ta); c.appendChild(foot);
  setTimeout(() => { try { ta.focus(); grow(); } catch (e) {} }, 30);
  return c;
}

/* ------------------------------------------------------------------ small helpers */
function prototypeLabel(label) {
  if (!prototypeMode || typeof label !== "string") return label;
  // The classic shell historically uses emoji prefixes. The prototype uses a single vector
  // icon language, so remove only leading decorative glyphs while preserving label text/HTML.
  return label.replace(/^[^A-Za-zÀ-ž0-9<]+/, "").trimStart();
}
function btn(label, fn, cls) {
  const b = el("button", "btn " + (cls || ""), prototypeLabel(label));
  b.addEventListener("click", fn);
  return b;
}
// seconds -> m:ss, or h:mm:ss once it runs past the hour (a longform voiceover does)
function clock(seconds) {
  const t = Math.max(0, Math.round(+seconds || 0));
  const h = Math.floor(t / 3600), m = Math.floor((t % 3600) / 60), s = t % 60;
  const mm = h ? String(m).padStart(2, "0") : String(m);
  return (h ? h + ":" : "") + mm + ":" + String(s).padStart(2, "0");
}
function linkBtn(label, href, cls) {
  const a = el("a", "btn " + (cls || ""), prototypeLabel(label)); a.href = href;
  // the Timeline Editor is a heavier page - show the loading overlay while it opens
  if (href.indexOf("/timeline") === 0)
    a.addEventListener("click", () => showLoading(T.open_timeline + "..."));
  return a;
}
function selectField(label, options, value, onChange) {
  const f = el("div", "fld");
  f.appendChild(el("label", "", esc(label)));
  const s = el("select");
  // Accept BOTH shapes. Every caller but two passes {value,label}; the Clip Short source card
  // passed ["value","label"] pairs, which produced <option value="undefined"> - and the step then
  // stored "undefined" as the chosen engine.
  (options || []).forEach(o => {
    const pair = Array.isArray(o);
    s.appendChild(new Option(pair ? o[1] : o.label, pair ? o[0] : o.value));
  });
  if (value && [...s.options].some(o => o.value === value)) s.value = value;
  else if (!value) { const d = (options || []).find(o => !Array.isArray(o) && o.selected); if (d) s.value = d.value; }
  // `true` = this is the initial sync while the card is still being BUILT. A handler that
  // re-renders must not do so here: renderSourceCard called renderAll() from inside itself and
  // the card stopped dead after the "Clip engine" caption, leaving the step with no Continue
  // button and the whole flow unusable.
  onChange(s.value, true);
  s.addEventListener("change", () => { onChange(s.value, false); persist(); });
  f.appendChild(s);
  return f;
}
// The design system's section block: a green mono cap over a 14px-gap body, separated by a
// hairline. Shared, so a config step never has to invent its own stack of bare fields.
function outSection(card, title, ...nodes) {
  const sec = el("div", "out-sec");
  sec.appendChild(el("div", "out-sec-cap", esc(title)));
  const body = el("div", "out-sec-body");
  nodes.forEach(n => n && body.appendChild(n));
  sec.appendChild(body);
  card.appendChild(sec);
  return sec;
}
function textField(label, value, placeholder, onChange, hint) {
  const f = el("div", "fld");
  f.appendChild(el("label", "", esc(label)));
  const i = el("input"); i.type = "text"; i.value = value || "";
  i.placeholder = placeholder || "";
  i.addEventListener("input", () => onChange(i.value));
  f.appendChild(i);
  if (hint) f.appendChild(el("small", "fld-hint", esc(hint)));
  return f;
}
function toggleField(label, value, onChange, disabled) {
  const l = el("label", "tgl");
  const i = el("input"); i.type = "checkbox"; i.checked = !!value; i.disabled = !!disabled;
  i.addEventListener("change", () => { onChange(i.checked); persist(); });
  l.appendChild(i); l.appendChild(el("span", "", esc(label)));
  return l;
}
function choiceButtons(options, value, onPick) {
  const w = el("div", "choices");
  (options || []).forEach(o => {
    const b = el("button", "choice" + (o.value === value ? " sel" : ""), esc(o.label));
    b.addEventListener("click", () => onPick(o.value));
    w.appendChild(b);
  });
  return w;
}
function labelFor(options, value) {
  const o = (options || []).find(x => x.value === value);
  return o ? o.label : (value || "-");
}
// slug/kind -> a short, human title for the sidebar job chip (drops timestamps + underscores)
function prettyJobTitle(s) {
  let t = String(s || "Run").replace(/_\d{6,}.*$/, "").replace(/[_-]+/g, " ").trim();
  t = t.replace(/\b\w/g, c => c.toUpperCase());
  return t.length > 26 ? t.slice(0, 25) + "…" : (t || "Run");
}
function labelForRunMode(m) {
  return { normal: T.run_normal, recut_existing_only: T.run_recut, recut_new_web_images: T.run_new_web,
    recut_regenerate_seedance: T.run_regen_clips, recut_recreate_speaker_clip: T.run_recreate_hook }[m] || m;
}
function firstVal(options) { const d = (options || []).find(o => o.selected) || (options || [])[0]; return d ? d.value : ""; }
function pickFile(accept, cb) {
  const i = el("input"); i.type = "file"; i.accept = accept || "";
  i.addEventListener("change", () => { if (i.files && i.files[0]) cb(i.files[0]); });
  i.click();
}
function dragDrop(node, cb) {
  node.addEventListener("dragover", e => { e.preventDefault(); node.classList.add("over"); });
  node.addEventListener("dragleave", () => node.classList.remove("over"));
  node.addEventListener("drop", e => {
    e.preventDefault(); node.classList.remove("over");
    const f = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
    if (f) cb(f);
  });
}
function errorCard(title, detail) {
  const c = card("err-card");
  c.appendChild(el("h3", "", esc(title)));
  if (detail) {
    const pre = el("pre", "tech-log", esc(detail)); c.appendChild(pre);
  }
  scrollDown();
  return c;
}
let _audio = null;
function ensureAudio() { if (!_audio) { _audio = new Audio(); } return _audio; }

/* Dedicated UI motion sound: deliberately quiet and short so it supports the lateral motion
   without reading like a content SFX or competing with the preview audio. */
let _uiWhoosh = null;
function prepareUiWhoosh() {
  if (!_uiWhoosh) {
    _uiWhoosh = new Audio("/file?path=" + encodeURIComponent("soundeffects/woosh-sfx.mp3"));
    _uiWhoosh.preload = "auto";
    _uiWhoosh.volume = 0.055;
  }
  return _uiWhoosh;
}
function playUiWhoosh() {
  try {
    const sound = prepareUiWhoosh();
    sound.pause(); sound.currentTime = 0; sound.playbackRate = 0.82;
    sound.play().catch(() => {});
  } catch (e) {}
}

/* Quiet global button feedback. Event delegation covers buttons created later by flows, jobs,
   project menus and dialogs without wiring every renderer separately. A small pool prevents rapid
   clicks from cutting off the preceding 212ms tick. */
const _uiClickPool = [];
let _uiClickCursor = 0;
function playGlobalUiClick() {
  try {
    if (!_uiClickPool.length) {
      for (let i = 0; i < 4; i++) {
        const sound = new Audio("/file?path=" + encodeURIComponent("soundeffects/ui-click-3.mp3"));
        sound.preload = "auto"; sound.volume = 0.16; _uiClickPool.push(sound);
      }
    }
    const sound = _uiClickPool[_uiClickCursor++ % _uiClickPool.length];
    sound.pause(); sound.currentTime = 0; sound.play().catch(() => {});
  } catch (e) {}
}
document.addEventListener("pointerdown", event => {
  const target = event.target.closest("button, a.button, [role='button'], input[type='button'], input[type='submit']");
  // New Creation is already a large cinematic selection surface. Click ticks made those cards
  // feel toy-like and competed with the shared zoom/arrival transition, so they stay silent.
  const silentCreationMenu = target && target.closest(".proto-launchpad");
  if (target && !silentCreationMenu && !target.disabled && target.getAttribute("aria-disabled") !== "true") playGlobalUiClick();
}, true);

/* short pleasant two-note chime (WebAudio, no asset) for render-finish + speech-ready */
let _actx = null;
function playNotification(kind) {
  try {
    _actx = _actx || new (window.AudioContext || window.webkitAudioContext)();
    if (_actx.state === "suspended") _actx.resume();
    const now = _actx.currentTime;
    const notes = kind === "speech" ? [[660, 0], [880, 0.14]] : [[784, 0], [1047, 0.13], [1319, 0.26]];
    const gain = _actx.createGain();
    gain.gain.value = 0.0001; gain.connect(_actx.destination);
    notes.forEach(([freq, t]) => {
      const o = _actx.createOscillator(); o.type = "sine"; o.frequency.value = freq;
      const g = _actx.createGain();
      const s = now + t;
      g.gain.setValueAtTime(0.0001, s);
      g.gain.exponentialRampToValueAtTime(0.16, s + 0.02);
      g.gain.exponentialRampToValueAtTime(0.0001, s + 0.26);
      o.connect(g); g.connect(_actx.destination);
      o.start(s); o.stop(s + 0.3);
    });
  } catch (e) {}
}

/* ---- loading overlay (opening Assets, the Timeline Editor, a project ...) ---- */
let _loadEl = null, _loadTimer = null;
function showLoading(text) {
  hideLoading();
  _loadEl = el("div", "load-overlay");
  _loadEl.innerHTML = `<div class="load-box"><span class="load-spin"></span><span>${esc(text || "Loading...")}</span></div>`;
  document.body.appendChild(_loadEl);
  _loadTimer = setTimeout(hideLoading, 15000);   // safety: never stuck forever
}
function openTimelineWithLoading(slug, title) {
  showLoading("Opening " + (title || "timeline") + "...");
  if (_loadEl) {
    _loadEl.classList.add("timeline-loading");
    const box = _loadEl.querySelector(".load-box");
    if (box) box.insertAdjacentHTML("beforeend", '<small>Preparing clips, audio and editor state</small>');
  }
  // Give the browser two paint frames before navigation so the loader is actually visible.
  requestAnimationFrame(() => requestAnimationFrame(() => {
    location.href = "/timeline?slug=" + encodeURIComponent(slug);
  }));
}
function hideLoading() {
  if (_loadTimer) { clearTimeout(_loadTimer); _loadTimer = null; }
  if (_loadEl) { _loadEl.remove(); _loadEl = null; }
}

/* Download the finished video where the USER chooses. Uses the File System Access API
   (showSaveFilePicker) so a real "Save as" dialog appears; falls back to a normal browser
   download (<a download>), and finally to the server-side copy into Downloads. */
async function downloadVideo(fileUrl, rawPath, name, btnEl) {
  const setLabel = (t) => { if (btnEl) btnEl.textContent = t; };
  const done = () => { if (btnEl) btnEl.disabled = false; };
  if (btnEl) btnEl.disabled = true;
  setLabel(T.download + "...");
  // 1) DESKTOP app (pywebview / WebView2): native "Save As" dialog. WebView2 ignores <a download>
  //    AND has no showSaveFilePicker, so this is the ONLY reliable path there - it must be tried
  //    FIRST (previously the <a download> fallback silently did nothing yet claimed success).
  try {
    if (window.pywebview && window.pywebview.api && window.pywebview.api.save_file) {
      const r = await window.pywebview.api.save_file(rawPath);
      if (r && r.ok) { setLabel("✓ " + T.download); done(); return; }
      if (r && r.cancelled) { setLabel("⬇ " + T.download); done(); return; }
      const sr = await jget("/save-render?path=" + encodeURIComponent(rawPath));
      setLabel(sr && sr.ok ? "✓ " + T.downloaded_to : T.err_generic); done(); return;
    }
  } catch (e) {}
  // 2) BROWSER: File System Access API "Save As"
  try {
    if (window.showSaveFilePicker) {
      const handle = await window.showSaveFilePicker({
        suggestedName: name,
        types: [{ description: "MP4 video", accept: { "video/mp4": [".mp4"] } }],
      });
      const resp = await fetch(fileUrl);
      const writable = await handle.createWritable();
      await resp.body.pipeTo(writable);
      setLabel("✓ Saved"); done(); return;
    }
  } catch (e) {
    if (e && e.name === "AbortError") { setLabel("⬇ " + T.download); done(); return; }
    // fall through to the fallbacks
  }
  // 3) plain browser download
  try {
    const a = document.createElement("a");
    a.href = fileUrl; a.download = name;
    document.body.appendChild(a); a.click(); a.remove();
    setLabel("✓ " + T.download); done(); return;
  } catch (e) {}
  // 4) server-side copy into Downloads
  try {
    const r = await jget("/save-render?path=" + encodeURIComponent(rawPath));
    setLabel(r && r.ok ? "✓ " + T.downloaded_to : T.err_generic);
  } catch (e) { setLabel(T.err_generic); }
  done();
}

/* legacy fragment shims: embedded server HTML may call these */
window.saveRender = function (encPath, btnEl) {
  const raw = decodeURIComponent(encPath || "");
  const name = (raw.split(/[\\/]/).pop()) || "short.mp4";
  downloadVideo("/file?path=" + encPath, raw, name, btnEl);
};
window.playClick = function () {};

/* ------------------------------------------------------------------ theme + drawer */
function applyTheme(t) {
  document.documentElement.dataset.theme = t;
  const toggle = $("theme-toggle");
  if (toggle) toggle.checked = t === "dark";
  const label = $("theme-toggle-label");
  if (label) label.textContent = t === "dark" ? "dark" : "light";
  try { localStorage.setItem("sl-chat-theme", t); } catch (e) {}
}
function applyPrototypeMode(on, rerender) {
  prototypeMode = !!on;
  document.documentElement.classList.toggle("prototype-ui", prototypeMode);
  const toggle = $("prototype-toggle");
  if (toggle) toggle.checked = prototypeMode;
  try { localStorage.setItem("sl-prototype-ui", prototypeMode ? "1" : "0"); } catch (e) {}
  try { localStorage.setItem("sl-prototype-ui-v2", prototypeMode ? "1" : "0"); } catch (e) {}
  if (rerender) { renderNav(); renderAll(); }
}
function wireChrome() {
  let savedTheme = "dark";
  try { savedTheme = localStorage.getItem("sl-chat-theme") || "dark"; } catch (e) {}
  applyTheme(savedTheme === "light" ? "light" : "dark");
  const savedPrototype = (() => {
    if (document.documentElement.classList.contains("design-v2")) return true;
    try {
      const value = localStorage.getItem("sl-prototype-ui-v2");
      return value == null ? true : value === "1";
    } catch (e) { return true; }
  })();
  applyPrototypeMode(savedPrototype, false);
  const prototypeToggle = $("prototype-toggle");
  if (prototypeToggle) prototypeToggle.addEventListener("change", () => applyPrototypeMode(prototypeToggle.checked, true));
  const themeToggle = $("theme-toggle");
  if (themeToggle) themeToggle.addEventListener("change", e => applyTheme(e.currentTarget.checked ? "dark" : "light"));
  $("sb-collapse").addEventListener("click", () => $("app").classList.toggle("sb-collapsed"));
  $("tb-menu").addEventListener("click", () => {
    const app = $("app");
    app.classList.remove("sb-collapsed");
    if (window.matchMedia("(max-width: 900px)").matches) {
      // narrow screens: the sidebar is a drawer over the content -> scrim behind it
      app.classList.add("sb-open"); $("sb-scrim").hidden = false;
    } else {
      // desktop: just show the sidebar again - NO scrim (it used to dim the whole
      // window until the next click)
      app.classList.remove("sb-open"); $("sb-scrim").hidden = true;
    }
  });
  $("sb-scrim").addEventListener("click", closeDrawer);
  document.addEventListener("keydown", e => { if (e.key === "Escape") closeDrawer(); });
  document.addEventListener("click", e => {
    if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    const a = e.target.closest('a[href*="/timeline?"]');
    if (!a || a.target === "_blank") return;
    try {
      const url = new URL(a.href, location.href);
      const slug = url.searchParams.get("slug");
      if (!slug) return;
      e.preventDefault();
      openTimelineWithLoading(slug, a.dataset.projectTitle || "timeline editor");
    } catch (err) {}
  }, true);
  wireSidebarResize();
}
// #125 - drag the sidebar's right edge to resize; width persists in localStorage.
function wireSidebarResize() {
  const grip = $("sb-resize"); if (!grip) return;
  const MIN = 210, MAX = 460;
  const apply = (w) => { document.documentElement.style.setProperty("--sb-w", w + "px"); };
  try { const s = parseInt(localStorage.getItem("sl-chat-sbw") || "", 10); if (s >= MIN && s <= MAX) apply(s); } catch (e) {}
  let startX = 0, startW = 0, dragging = false;
  const move = (e) => {
    if (!dragging) return;
    const w = Math.max(MIN, Math.min(MAX, startW + (e.clientX - startX)));
    apply(w);
  };
  const up = () => {
    if (!dragging) return;
    dragging = false; document.body.classList.remove("sb-resizing");
    document.removeEventListener("pointermove", move); document.removeEventListener("pointerup", up);
    try { localStorage.setItem("sl-chat-sbw", parseInt(getComputedStyle($("sidebar")).width, 10)); } catch (e) {}
  };
  grip.addEventListener("pointerdown", (e) => {
    // resizing only makes sense on desktop where the sidebar is docked, not the mobile drawer
    if (window.matchMedia("(max-width: 900px)").matches) return;
    e.preventDefault(); dragging = true; startX = e.clientX;
    startW = parseInt(getComputedStyle($("sidebar")).width, 10) || 260;
    document.body.classList.add("sb-resizing");
    document.addEventListener("pointermove", move); document.addEventListener("pointerup", up);
  });
}
function closeDrawer() { $("app").classList.remove("sb-open"); $("sb-scrim").hidden = true; }

/* ------------------------------------------------------------------ boot + deep links + restore */
async function boot() {
  wireChrome(); wireComposer(); renderNav();
  refreshSidebar(); jobsTimer = setInterval(refreshSidebar, 12000);

  const init = BOOT.initial || {};
  // The app ALWAYS opens on a fresh chat (mode menu). Persisted chat sessions are never
  // auto-restored on load - a stale "Your Short is ready" or half-finished setup must not
  // reappear. Only explicit deep links (/job?id=, /?project=, /sfx, /assets) set a state.
  if (init.new) resetToMode();
  if (init.view === "assets") { S.view = "assets"; assetsInitialQuery = init.q || ""; }
  if (init.flow) { S.flow = init.flow; S.step = stepsFor(init.flow)[0]; S.completed = []; S.view = "chat"; S.jobId = null; }
  if (init.job) { S.jobId = init.job; S.jobStatus = "running"; S.view = "chat"; if (!S.flow) S.flow = "script"; S.step = "review"; S.completed = stepsFor("script").slice(0, -1); }
  if (init.project) { await loadProject(init.project); return; }
  renderAll();
}
boot();
