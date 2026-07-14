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

async function jget(url) { const r = await fetch(url); return r.json(); }

/* single-frame project thumbnail (hook/opening) + a master-tool badge overlay */
function projThumb(p, cls) {
  // Overlay label + colored border: failed (red) wins, else an SFX/VFX-Master upload gets its
  // own colored tag; an ordinary generated/scraped project gets no overlay.
  const kind = p.running ? "running" : p.failed ? "failed" : (p.kind || p.preview_kind || "");
  const txt = kind === "running" ? "running" : kind === "failed" ? "failed" : kind === "sfx" ? "SFX" : kind === "vfx" ? "VFX" : "";
  const tag = txt ? `<span class="pv-tag pv-tag-${kind}">${txt}</span>` : "";
  const inner = p.thumb_url
    ? `<img loading="lazy" src="${esc(p.thumb_url)}" alt="">`
    : (p.video_url ? `<video muted preload="none" src="${esc(p.video_url)}"></video>` : "");
  return `<span class="pv-wrap ${cls || ""}${kind ? " pv-" + kind : ""}">${inner}${tag}</span>`;
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
  if (!v.scraping_engine) v.scraping_engine = "v2";
  if (!v.script_relevancy) v.script_relevancy = "70";
  if (!v.scrape_sort) v.scrape_sort = "ALL";
  if (!v.scrape_platforms) v.scrape_platforms = "tiktok,x,instagram";
  if (!v.sfx_amount) v.sfx_amount = "medium";
  if (!v.vfx_amount) v.vfx_amount = "medium";
  if (st.add_visual_effects === undefined) v.add_visual_effects = true;   // arrows on by default
  if (!v.pipeline_version) v.pipeline_version = "v0.2";   // version chooser removed; always v0.2
  if (!v.speaker_name) v.speaker_name = "Narrator";
  v.loaded_project_mode = v.loaded_project_mode || "normal";
  v.loaded_project_source = "";
  return v;
};

let S = {
  flow: null,              // script | viraltrans | reddit | longform | sfx | visual | captions | project
  step: "mode",           // current step id
  values: DEFAULT_VALUES(),
  master: {},              // master-tool settings (reasoning_model, sfx_amount, vfx_amount, add_characters, caption_*)
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
  viraltrans: ["topic", "review"],
  reddit: ["discover", "pick"],
  longform: ["script", "settings"],
  sfx: ["upload", "settings"],
  visual: ["upload", "settings"],
  captions: ["upload", "settings"],
  enhance: ["choose"],
  project: ["summary"],
};
function isCultureFacts() { return S.flow === "script" && !!S.values.culture_facts_mode; }
function isVisualsFromScript() { return S.flow === "script" && !!S.values.visuals_from_script_mode; }
function applyCultureFactsPreset() {
  S.values.culture_facts_mode = true;
  S.values.clip_source = "scrape";
  S.values.scraping_engine = "v2";
  S.values.enable_speaker_hook = false;
  S.values.speaker_image_path = "";
  FILES.speaker_image_file = null;
  S.values.out_web_images = false;
  S.values.out_wikimedia = false;
  S.values.out_gpt_images = false;
  S.values.out_video_clips = true;
}
function stepsFor(flow) {
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

  if (prototypeMode && S.jobId) {
    renderJobSection(); scrollDown(); return;
  }

  ({ script: renderScriptFlow, viraltrans: renderViralFlow, reddit: renderRedditFlow,
     longform: renderLongformFlow, sfx: () => renderMasterFlow("sfx"),
     visual: () => renderMasterFlow("visual"), captions: () => renderMasterFlow("captions"),
     enhance: renderEnhanceFlow,
     project: renderProjectFlow }[S.flow] || renderModeMenu)();

  if (prototypeMode && !S.jobId) decoratePrototypeFlow();
  if (S.jobId) renderJobSection();
  scrollDown();
}

// Short chip labels for the step carousel (one per known step id)
const STEP_SHORT = {
  script: "Story", voice: "Voice", source: "Footage", reasoning: "Director",
  outputs: "Finish", review: "Review", topic: "Topic", discover: "Discover",
  pick: "Select", upload: "Source", settings: "Settings", choose: "Upgrade",
  summary: "Project", hook: "Opening", version: "Edit",
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
function stepCopy(step) {
  return ({
    script:["STORY", "Add your script", "Paste it or generate a fresh one."],
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
    settings:["SETTINGS", "Direct the enhancement", "Choose the intensity and model."],
    choose:["UPGRADE", "Choose an enhancement", "Pick one production pass."],
    summary:["PROJECT", "Project overview", "Choose the next action."],
  }[step] || ["SETTINGS", "Configure this step", "Make your choices and continue."]);
}
let _lastStepIdx = null;   // for slide direction between steps
function decoratePrototypeFlow() {
  chat.querySelectorAll(":scope > .msg").forEach(m => m.classList.add("proto-flow-context"));
  const cards = chat.querySelectorAll(":scope > .chat-card");
  if (!cards.length) return;
  const active = cards[cards.length - 1];
  active.classList.add("proto-active-card", "proto-step-surface", `proto-step-${S.step || "default"}`);
  active.dataset.step = S.step || "default";
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
  { id: "viraltrans", grp: 0, ico: "🧪", t: T.mode_viral_t, d: T.mode_viral_d },
  { id: "reddit", grp: 0, ico: "💬", t: T.mode_reddit_t, d: T.mode_reddit_d },
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
    if (g === 1) visibleModes.unshift({ id:"enhance", ico:"✦", t:"Enhance video", d:"Choose SFX Master, Visual Master or Caption Master after opening." });
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
      <h1>Pick the format.<br><em>Shape the story.</em></h1>
      <p>Choose a starting point, configure your production, and let AI handle the rest.</p>
    </header>
    <section class="proto-primary-tools" aria-label="Production tools">
      <button type="button" class="proto-tool proto-creator-card proto-tool-culture" data-mode="culture">
        <video id="proto-culture-preview" muted autoplay loop playsinline preload="auto" poster="/file?path=static%2Fpreviews%2Fclip_short_poster.jpg" src="/file?path=static%2Fpreviews%2Fclip_short_real_20s_hd.mp4" aria-hidden="true"></video>
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
      <button type="button" class="proto-tool proto-creator-card proto-tool-reddit" data-mode="reddit">
        <video muted autoplay loop playsinline preload="auto" poster="/file?path=static%2Fpreviews%2Fstory_flow_poster.jpg" src="/file?path=static%2Fpreviews%2Fstory_flow_20s_hd.mp4" aria-hidden="true"></video>
        <span class="proto-culture-shade"></span>
        <span class="proto-reddit-caption"><b>r/AskReddit</b><span>What is a secret you were never supposed to find out?</span></span>
        <span class="proto-tool-copy"><small>STORY + GAMEPLAY</small><strong>Reddit Story</strong>
          <em>Turn a thread into a narrated short over Minecraft parkour.</em></span>
        <span class="proto-tool-go">Find a story ${protoIcon("arrow")}</span>
      </button>
    </section>
    <section class="proto-secondary-tools" aria-label="More creation modes">
      <button type="button" data-mode="enhance">${protoIcon("visual")}<span><b>Enhance video</b><small>SFX, visual effects or captions</small></span></button>
      <button type="button" data-mode="viraltrans">${protoIcon("flask")}<span><b>Viral Transcriber</b><small>Rebuild a proven format</small></span></button>
      <button type="button" data-mode="longform">${protoIcon("longform")}<span><b>Longform Visuals</b><small>Illustrate longer narration</small></span></button>
    </section>
    <section class="proto-home-section">
      <header><div><small>CONTINUE WORKING</small><h2>Recent projects</h2></div><button type="button" data-open-assets>View all ${protoIcon("arrow")}</button></header>
      <div class="proto-project-grid" id="proto-project-grid"><div class="proto-skeleton"></div><div class="proto-skeleton"></div><div class="proto-skeleton"></div></div>
    </section>
    <section class="proto-live-dock" id="proto-live-dock" hidden aria-live="polite"></section>`;
  chat.appendChild(home);
  // Prototype creation menu: keep the four distinct production modes only.
  home.querySelector('[data-mode="script"]')?.remove();
  home.querySelector('[data-mode="viraltrans"]')?.remove();
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
  const story = home.querySelector('[data-mode="reddit"]');
  if (story) {
    story.querySelector(".proto-reddit-caption")?.remove();
  }
  labelCard("reddit", "STORY B-ROLL", "Story Flow",
    "Tell Reddit, 4chan or other stories over satisfying background footage.", "Create Story Flow");
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
    home.querySelector(".proto-primary-tools").appendChild(longform);
  }
  const enhance = home.querySelector('[data-mode="enhance"]');
  if (enhance) {
    const upgradeSection = enhance.parentElement;
    if (upgradeSection) {
      upgradeSection.className = "proto-upgrade-section";
      const upgradeHead = el("header", "proto-section-heading");
      upgradeHead.innerHTML = `<h2>Upgrade your video</h2>`;
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
  home.querySelector("[data-open-assets]").addEventListener("click", () => showAssets(false));
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
      const status = p.running ? "Run in progress" : p.failed ? "Needs attention" : p.has_timeline ? "Ready to edit" : "In progress";
      b.innerHTML = `<span class="proto-project-poster">${preview}${p.kind ? `<i>${esc(p.kind)}</i>` : ""}</span>
        <span class="proto-project-copy"><b>${esc(p.title || p.slug)}</b><small>${esc(status)} · ${esc(fmtDate(p.edited))}</small></span>${protoIcon("more")}`;
      b.addEventListener("click", () => loadProject(p.slug));
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
      b.addEventListener("click", () => startJob(j.id)); dock.appendChild(b);
    });
  } catch (e) { if (dock) dock.hidden = true; }
}
function selectMode(id) {
  const culture = id === "culture";
  const visualScript = id === "visualscript";
  S.flow = (culture || visualScript) ? "script" : id; S.completed = [];
  S.values.culture_facts_mode = culture;
  S.values.visuals_from_script_mode = visualScript;
  if (culture) applyCultureFactsPreset();
  else if (id === "script" || visualScript) {
    S.values.clip_source = "generate";
    S.values.scraping_engine = "v2";
  }
  S.step = stepsFor(S.flow)[0];
  renderAll(); persist();
}

/* ------------------------------------------------------------------ FLOW: A Video from a Script */
function renderScriptFlow() {
  const done = (s) => S.completed.includes(s);
  msgU(esc(T.mode_script_t));

  // step: script
  msgA(esc(T.send_script) + `<div class="card-note">${esc(T.script_hint)}</div>`);
  if (done("script")) {
    const sc = S.values.script || "";
    msgU(esc(sc.length > 220 ? sc.slice(0, 220) + "…" : sc), "script");
  } else if (S.step === "script") {
    const c = card("script-config-card");
    const ta = el("textarea", "script-box"); ta.id = "script-edit";
    ta.placeholder = T.script_placeholder; ta.value = S.values.script || "";
    c.appendChild(ta);
    const sizeScriptBox = () => {
      ta.style.height = "auto";
      // compact: grows with content but starts small and never dominates the card
      ta.style.height = Math.min(Math.max(150, ta.scrollHeight + 2), Math.round(window.innerHeight * .42)) + "px";
    };
    ta.addEventListener("input", sizeScriptBox);
    requestAnimationFrame(sizeScriptBox);
    // Hook + impact-word are marked RIGHT HERE on the same script field (no separate step, so the
    // text can never desync). Select text in the box above, then Mark hook / Mark impact.
    {
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
    {
      const row = el("div", "script-gen-row");
      const ti = document.createElement("input");
      ti.type = "text"; ti.placeholder = T.gen_topic_ph; ti.style.flex = "1";
      ti.value = S.values.gen_topic || "";
      ti.addEventListener("input", () => { S.values.gen_topic = ti.value; });
      row.appendChild(ti);
      const instructionWrap = el("label", "generator-instructions");
      instructionWrap.innerHTML = `<span>GENERATOR INSTRUCTIONS <em>OPTIONAL</em></span>`;
      const instructionInput = el("textarea", "generator-instructions-input");
      instructionInput.rows = 3;
      instructionInput.placeholder = "e.g. Make it less exaggerated, keep the facts broader, or structure it as three distinct facts.";
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
            }) });
          const d = await r.json();
          if (!d.ok) throw new Error(d.error || "no script");
          ta.value = d.script; S.values.script = d.script;
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
              ta.value = it.script; S.values.script = it.script; persist(); box.remove();
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
    {
      const nrow = el("div", "script-narrator");
      nrow.appendChild(el("span", "sn-lbl", esc(T.tts_voice || "Narrator")));
      const vsel = el("select");
      (OPT.tts_voice || []).forEach(o => vsel.appendChild(new Option(o.label, o.value)));
      if (S.values.tts_voice && [...vsel.options].some(o => o.value === S.values.tts_voice)) vsel.value = S.values.tts_voice;
      S.values.tts_voice = vsel.value;
      vsel.addEventListener("change", () => { S.values.tts_voice = vsel.value; persist(); });
      nrow.appendChild(vsel);
      nrow.appendChild(btn("▶", () => {
        const a = ensureAudio(); a.src = BOOT.voices_preview + encodeURIComponent(S.values.tts_voice || "");
        a.play().catch(() => {});
      }, "ghost small"));
      const msel = el("select");
      (OPT.tts_model || []).forEach(o => msel.appendChild(new Option(o.label, o.value)));
      if (S.values.tts_model && [...msel.options].some(o => o.value === S.values.tts_model)) msel.value = S.values.tts_model;
      S.values.tts_model = msel.value;
      msel.addEventListener("change", () => { S.values.tts_model = msel.value; persist(); });
      nrow.appendChild(msel);
      c.appendChild(nrow);
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
    foot.appendChild(el("span", "spacer"));
    foot.appendChild(btn(T.continue, () => {
      const v = ta.value.trim();
      if (!v) { ta.focus(); return; }
      S.values.script = v;
      if (S.values.hook_text && !v.includes(S.values.hook_text)) S.values.hook_text = "";
      if (S.values.impact_word && !v.toLowerCase().includes(S.values.impact_word.toLowerCase())) S.values.impact_word = "";
      completeStep("script", "source");
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
  if (done("source")) {
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
      foot.appendChild(btn(T.back, () => editStep("source"), "ghost"));
      foot.appendChild(btn("Continue", () => completeStep("reasoning", "outputs"), "primary"));
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
  if (done("outputs")) {
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
    c.appendChild(el("div", "card-note", "Clip Short uses relevance-first Scrape V2 across TikTok, X and Instagram. Tune its search below."));
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
    S.values.scraping_engine = "v2";
    // relevancy
    wrap.appendChild(el("div", "card-cap", esc(T.script_relevancy)));
    const rr = el("div", "range-row");
    const rg = el("input"); rg.type = "range"; rg.min = 0; rg.max = 100; rg.step = 5;
    rg.value = S.values.script_relevancy || "70";
    const rv = el("span", "range-val", (S.values.script_relevancy || "70") + "%");
    rg.addEventListener("input", () => { S.values.script_relevancy = rg.value; rv.textContent = rg.value + "%"; persist(); });
    rr.appendChild(rg); rr.appendChild(rv); wrap.appendChild(rr);
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
    // connections + per-platform "search this platform" toggle (the slider sits next to Reconnect)
    if (!S.values.scrape_platforms) S.values.scrape_platforms = "tiktok,x,instagram";
    wrap.appendChild(el("div", "card-cap", esc(T.connections)));
    wrap.appendChild(connectionRow("tiktok", T.connect_tiktok, "/tiktok-status", "/tiktok-login", true));
    wrap.appendChild(connectionRow("x", T.connect_x, "/twitter-status", "/twitter-login", true));
    wrap.appendChild(connectionRow("instagram", T.connect_instagram || "Connect Instagram", "/instagram-status", "/instagram-login", true));
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
  (OPT.tts_voice || []).forEach(o => vsel.appendChild(new Option(o.label, o.value)));
  if (S.values.tts_voice && [...vsel.options].some(o => o.value === S.values.tts_voice)) vsel.value = S.values.tts_voice;
  S.values.tts_voice = vsel.value;
  vsel.addEventListener("change", () => { S.values.tts_voice = vsel.value; persist(); });
  const prev = btn("▶ " + T.preview, () => {
    const a = ensureAudio(); a.src = BOOT.voices_preview + encodeURIComponent(S.values.tts_voice || "");
    a.play().catch(() => {});
  }, "ghost small");
  voiceInline.appendChild(vsel); voiceInline.appendChild(prev);
  voiceFld.appendChild(voiceInline);
  const row = el("div", "fld-row");
  row.appendChild(voiceFld);
  row.appendChild(selectField(T.tts_model, OPT.tts_model, S.values.tts_model, v => S.values.tts_model = v));
  c.appendChild(row);
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
    const sec = el("div", "out-sec");
    sec.appendChild(el("div", "out-sec-cap", esc(title)));
    const body = el("div", "out-sec-body");
    nodes.forEach(n => n && body.appendChild(n));
    sec.appendChild(body); c.appendChild(sec);
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
  // Visual effects (arrows/callouts baked in-render + optional meme & neko reaction layers)
  const vg = el("div", "tgl-grid");
  vg.appendChild(toggleField("Arrows & callouts", S.values.add_visual_effects !== false, v => S.values.add_visual_effects = v));
  vg.appendChild(toggleField("Meme reactions", !!S.values.add_meme_reactions, v => S.values.add_meme_reactions = v));
  vg.appendChild(toggleField("Neko reactions", !!S.values.add_neko_reactions, v => S.values.add_neko_reactions = v));
  section("Visual effects", vg, amountSliderField("Amount", S.values.vfx_amount || "medium", v => S.values.vfx_amount = v));
  // Sound
  section("Sound", grid([["out_sfx", "Sound effects"], ["out_transition_sfx", "Transition SFX"]]),
    amountSliderField("Amount", S.values.sfx_amount, v => S.values.sfx_amount = v));
  // Captions
  section("Captions", grid([["out_captions", "Word-by-word captions"]]));
  // Speech approval
  const halt = toggleField(T.halt_after_speech, S.values.halt_after_speech, v => S.values.halt_after_speech = v);
  section("Speech", halt, el("div", "out-sec-hint", "Pause to approve or re-do the voice before the run finishes."));
  const foot = el("div", "card-foot");
  foot.appendChild(btn(T.back, () => editStep("reasoning"), "ghost"));
  foot.appendChild(el("span", "spacer"));
  foot.appendChild(btn(T.continue, () => completeStep("outputs", "review"), "primary"));
  c.appendChild(foot);
}

function renderEnhanceFlow() {
  msgA("What do you want to enhance?");
  const c = card("enhance-picker");
  const grid = el("div", "mode-grid");
  [
    ["sfx", protoIcon("sound"), "SFX Master", "Add transition, reaction and word-triggered sound effects."],
    ["visual", protoIcon("visual"), "Visual Master", "Add arrows, focus cues and motion-aware visual effects."],
    ["captions", protoIcon("captions"), "Caption Master", "Generate or restyle captions for an existing video."],
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
function visibleOutputFields() {
  return isCultureFacts()
    ? OUTPUT_FIELDS.filter(([k]) => ["out_sfx", "out_transition_sfx", "out_captions"].includes(k))
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
  const rows = [
    ["Mode", isCultureFacts() ? "Clip Short" : isVisualsFromScript() ? "AI Short" : T.mode_script_t, null],
   ["Visual source", isCultureFacts() ? "TikTok, X & Instagram · Scrape V2" : (S.values.clip_source === "scrape" ? T.src_scrape : T.src_generate), isCultureFacts() ? null : "source"],
  ];
  if (S.values.clip_source === "scrape") {
    if (!isCultureFacts()) rows.push([T.scrape_engine, "Scrape V2", "source"]);
    rows.push([T.script_relevancy, (S.values.script_relevancy || "70") + "%", isCultureFacts() ? null : "source"]);
    rows.push(["Search platforms", (S.values.scrape_platforms || "tiktok,x,instagram").split(",").map(p => p === "x" ? "X" : p.charAt(0).toUpperCase() + p.slice(1)).join(", "), "source"]);
    if (S.values.scrape_terms) rows.push(["Search terms", S.values.scrape_terms, isCultureFacts() ? null : "source"]);
  } else {
    rows.push([T.video_model, labelFor(OPT.video_model, S.values.video_model), "source"]);
    rows.push([T.image_model, labelFor(OPT.image_model, S.values.image_model), "source"]);
  }
  rows.push(["Reasoning model", labelFor(OPT.reasoning_model, S.values.reasoning_model), "reasoning"]);
  rows.push(["Voice", S.values.tts_voice + " · " + labelFor(OPT.tts_model, S.values.tts_model), "script"]);
  if (!isCultureFacts()) rows.push(["Speaker video", S.values.enable_speaker_hook ? "On" : "Off", "script"]);
  rows.push(["Hook", S.values.hook_text ? T.hook_marked : T.no_hook, "script"]);
  if (S.values.impact_word) rows.push(["Impact word", S.values.impact_word, "script"]);
  var vfxLayers = [S.values.add_visual_effects !== false ? "arrows" : null,
                   S.values.add_meme_reactions ? "memes" : null,
                   S.values.add_neko_reactions ? "nekos" : null].filter(Boolean);
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
  if (FILES.speaker_image_file) fd.append("speaker_image_file", FILES.speaker_image_file);
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
  const titles = { sfx: T.mode_sfx_t, visual: T.mode_vfx_t, captions: T.mode_captions_t };
  const uploadQ = { sfx: T.sfx_upload_q, visual: T.vfx_upload_q, captions: T.cap_upload_q };
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
      c.appendChild(toggleField(T.neko_toggle, S.master.add_characters !== false,
        v => S.master.add_characters = v));
      c.appendChild(toggleField("Add meme reactions", S.master.add_memes !== false,
        v => S.master.add_memes = v));
    } else {
      c.appendChild(selectField(T.cap_max_words, OPT.caption_max_words, S.master.caption_max_words,
        v => S.master.caption_max_words = v));
      c.appendChild(selectField(T.cap_center_y, OPT.caption_center_y, S.master.caption_center_y,
        v => S.master.caption_center_y = v));
    }
    const labels = { sfx: "🔊 " + T.add_sfx, visual: "➜ " + T.add_arrows, captions: "💬 " + T.add_captions };
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
    if (S.master.add_characters !== false) fd.append("add_characters", "on");
    if (S.master.add_memes !== false) fd.append("add_memes", "on");
  } else {
    fd.append("caption_max_words", S.master.caption_max_words || firstVal(OPT.caption_max_words) || "4");
    fd.append("caption_center_y", S.master.caption_center_y || firstVal(OPT.caption_center_y) || "0.62");
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

/* ------------------------------------------------------------------ FLOW: viral transformation */
function renderViralFlow() {
  msgU(esc(T.mode_viral_t));
  msgA(esc(T.viral_topic_q));
  if (S.completed.includes("topic")) {
    msgU(esc(S.topic), "topic");
    if (S.step === "review" && !S.jobId) {
      const c = card();
      c.appendChild(el("h3", "", esc(T.ready_create)));
      const g = el("div", "sum-grid");
      g.appendChild(el("span", "k", "Mode")); g.appendChild(el("span", "v", esc(T.mode_viral_t))); g.appendChild(el("span"));
      g.appendChild(el("span", "k", "Topic")); g.appendChild(el("span", "v", esc(S.topic))); g.appendChild(el("span"));
      c.appendChild(g);
      const foot = el("div", "card-foot");
      foot.appendChild(btn(T.back, () => editStep("topic"), "ghost"));
      foot.appendChild(el("span", "spacer"));
      foot.appendChild(btn("🧪 " + T.generate, submitViral, "primary"));
      c.appendChild(foot);
    }
    setComposer("off");
  } else {
    // topic chips from the legacy page (single source of truth)
    const c = card(); const chips = el("div", "choices"); c.appendChild(chips);
    fetch("/viraltrans?legacy_ui=1").then(r => r.text()).then(html => {
      const re = /data-topic="([^"]+)"/g; let m; const seen = new Set();
      while ((m = re.exec(html))) {
        if (seen.has(m[1])) continue; seen.add(m[1]);
        const b = el("button", "choice", esc(m[1]));
        b.addEventListener("click", () => { S.topic = m[1]; completeStep("topic", "review"); });
        chips.appendChild(b);
      }
    }).catch(() => {});
    textInputCard(T.topic_placeholder, (v) => { S.topic = v; completeStep("topic", "review"); });
  }
}
async function submitViral() {
  const c = card(); c.appendChild(el("div", "dots", "<i></i><i></i><i></i>"));
  try {
    const body = new URLSearchParams({ topic: S.topic });
    const r = await fetch("/viraltrans-generate", { method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" }, body });
    const jid = new URL(r.url, location.href).searchParams.get("id");
    if (!jid) throw new Error("no job id");
    startJob(jid);
  } catch (e) { c.remove(); errorCard(T.err_generic, String(e)); }
}

/* ------------------------------------------------------------------ FLOW: reddit story */
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
  msgU(esc(T.mode_longform_t));
  msgA(esc(T.longform_script_q));
  const script = (S.longform.script || "").trim();
  if (S.completed.includes("script") && script) {
    msgU(esc(script.length > 220 ? script.slice(0, 220) + "…" : script), "script");
  } else if (S.step === "script" || !S.completed.includes("script")) {
    const c = card();
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
    if (OPT.longform_tts.length) c.appendChild(selectField(T.longform_tts, OPT.longform_tts, S.longform.tts_model, v => S.longform.tts_model = v));
    if (OPT.longform_reasoning.length) {
      c.appendChild(selectField(T.longform_reasoning, OPT.longform_reasoning, S.longform.reasoning_model, v => { const changed=!!S.longform.reasoning_model&&S.longform.reasoning_model!==v; S.longform.reasoning_model = v; S.longform.reasoning_mode = reasoningOptions(v, S.longform.reasoning_mode).value; if(changed)setTimeout(renderAll,0); }));
      appendReasoningField(c, S.longform.reasoning_model || firstVal(OPT.longform_reasoning), S.longform);
    }
    // "Halt after speech": pause after TTS so every voiceover part can be approved/declined
    {
      const row = el("label", "toggle-row");
      const cb = document.createElement("input"); cb.type = "checkbox";
      cb.checked = S.longform.halt_after_speech !== false;   // default ON
      cb.addEventListener("change", () => { S.longform.halt_after_speech = cb.checked; persist(); });
      row.appendChild(cb);
      row.appendChild(el("span", "", esc(T.longform_halt_speech)));
      c.appendChild(row);
    }
    c.appendChild(el("div", "card-cap", esc(T.connections)));
    c.appendChild(connectionRow("higgsfield", T.connect_higgsfield, "/higgsfield-status", "/higgsfield-login"));
    const foot = el("div", "card-foot");
    foot.appendChild(btn(T.back, () => editStep("script"), "ghost"));
    foot.appendChild(el("span", "spacer"));
    foot.appendChild(btn("🎬 " + T.create_longform, submitLongform, "primary"));
    c.appendChild(foot);
  }
  setComposer("off");
}
async function submitLongform() {
  const fd = new FormData();
  fd.append("script", S.longform.script || "");
  fd.append("tts_model", S.longform.tts_model || firstVal(OPT.longform_tts) || "pro");
  fd.append("reasoning_model", S.longform.reasoning_model || firstVal(OPT.longform_reasoning) || "anthropic/claude-opus-4.8");
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
async function loadProject(slug) {
  try {
    showLoading(T.project_loaded + "...");
    // Detach the previous completed job so its "Your short is ready" card cannot leak
    // into the newly selected project's deterministic render.
    S.jobId = null; S.jobStatus = "";
    announcedPhases = []; lastProgressHTML = lastMediaHTML = lastOutputsHTML = "";
    lastAssignedKey = "";
    history.replaceState(null, "", "/?project=" + encodeURIComponent(slug));
    const d = await jget("/project-preset?slug=" + encodeURIComponent(slug));
    S.projectSlug = d.slug; S.projectTitle = d.title || d.slug;
    const st = d.state || {};
    Object.keys(st).forEach(k => {
      if (MAN.run.text.includes(k)) S.values[k] = st[k] != null ? String(st[k]) : S.values[k];
      if (MAN.run.check.includes(k)) S.values[k] = !!st[k];
      if (MAN.run.state_hidden.includes(k)) S.values[k] = !!st[k];
    });
    S.values.loaded_project_source = d.slug;
    // metadata for the summary
    const pl = await jget("/projects-list");
    S._projInfo = (pl.projects || []).find(p => p.slug === slug) || {};
    S.flow = "project"; S.step = "summary"; S.completed = [];
    S.view = "chat";
    hideLoading();
    renderAll(); persist();
  } catch (e) { hideLoading(); errorCard(T.err_generic, String(e)); }
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
function startJob(jobId) {
  S.jobId = jobId; S.jobStatus = "running"; S.draft = false;
  announcedPhases = []; lastProgressHTML = lastMediaHTML = lastOutputsHTML = ""; lastAssignedKey = "";
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
  // speech approval placeholder (interactive gate, right above the run box)
  const speech = el("div"); speech.id = "job-speech"; chat.appendChild(speech);
  // ---- hero progress card: FURTHEST DOWN, big prominent bar, pulsing status + elapsed
  const c = card("prog-card"); c.id = "job-card";
  const head = el("div", "prog-head");
  const dot = el("span", "status-pulse"); dot.id = "job-dot";
  head.appendChild(dot);
  head.appendChild(el("b", "", "Creating your Short"));
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
  pollScrapeBrowser(); scrapePreviewTimer=setInterval(pollScrapeBrowser,3000);
  setComposer("off");
}
function stopPolling() { if (pollTimer) { clearInterval(pollTimer); pollTimer = null; } if(scrapePreviewTimer){clearInterval(scrapePreviewTimer);scrapePreviewTimer=null;} }

async function pollScrapeBrowser(){
  const monitor=$("scrape-monitor"),card=$("scrape-browser-card"),image=$("scrape-browser-image"),meta=$("scrape-browser-meta");
  const accepted=$("last-accepted-card"),video=$("last-accepted-video"),acceptedMeta=$("last-accepted-meta"),acceptedQuery=$("last-accepted-query"),empty=$("last-accepted-empty"),poster=$("last-accepted-poster");
  if(!card||!image)return;
  try{
    const d=await jget('/scrape-browser-status');
    card.hidden=!d.available;
    if(monitor)monitor.hidden=!d.available&&!d.last_accepted_url;
    meta.textContent=[d.platform,d.query,d.sort].filter(Boolean).join(' · ');
    if(d.available&&String(image.dataset.version||'')!==String(d.version)){
      image.dataset.version=String(d.version); image.src='/scrape-browser-preview?v='+encodeURIComponent(d.version);
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
function renderAssignedMedia(items) {
  const mw = $("job-media");
  if (!mw) return;
  const key = (items || []).map(i => i.url).join("|");
  if (key === lastAssignedKey) return;
  lastAssignedKey = key;
  mw.innerHTML = "";
  if (!items || !items.length) return;
  if (prototypeMode) mw.appendChild(el("div", "assigned-head", `<span>ASSIGNED FOOTAGE</span><b>${items.length} clip${items.length === 1 ? "" : "s"} chosen for your scenes</b>`));
  else typedMsg(mw, `Assigned footage — ${items.length} clip${items.length === 1 ? "" : "s"} chosen for your scenes.`);
  const c = el("div", "chat-card am-card");
  const grid = el("div", "am-grid");
  items.forEach(it => {
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
    t.appendChild(el("span", "am-name", esc(it.name.replace(/^scraped_/, "").slice(0, 22))));
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

async function pollJob() {
  if (!S.jobId) return;
  let d;
  try { d = await jget("/job-status?id=" + encodeURIComponent(S.jobId)); }
  catch (e) { return; }
  if (!d.exists) {
    stopPolling(); S.jobStatus = "missing";
    errorCard(T.err_no_job, ""); persist(); return;
  }
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
  // (phase-message chat bubbles removed - the user follows progress in the run box / tech log)
  // longform per-part speech approval ("Halt after speech" in the longform creator)
  const lfp = $("job-speech");
  if (lfp && d.status === "awaiting_approval" && (d.lf_parts || []).length) {
    const sig = JSON.stringify((d.lf_parts || []).map(p => [p.index, p.state, p.url]));
    if (lfp.dataset.lfSig !== sig) {
      lfp.dataset.lfSig = sig; lfp.innerHTML = "";
      const m = el("div", "msg assistant"); m.appendChild(el("div", "bubble", esc(T.lf_parts_ready))); lfp.appendChild(m);
      const c = el("div", "chat-card"); lfp.appendChild(c);
      (d.lf_parts || []).forEach(p => {
        const row = el("div", "lf-part");
        row.appendChild(el("div", "card-cap", esc(T.lf_part) + " " + (p.index + 1) +
          (p.state === "approved" ? " · ✓ " + esc(T.lf_approved) :
           p.state === "regenerating" ? " · ↻ " + esc(T.lf_regenerating) : "")));
        row.appendChild(el("div", "lf-part-text", esc((p.text || "").slice(0, 220))));
        if (p.url && p.state !== "regenerating") {
          const au = el("audio", "inline-audio"); au.controls = true; au.src = p.url; row.appendChild(au);
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
  // speech approval
  const sp = $("job-speech");
  if (sp) {
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
      [["", "Keep current (" + curSpeed.toFixed(2) + "x)"],
       ["1.0", "1.00x"], ["1.15", "1.15x"], ["1.2", "1.20x"],
       ["1.3", "1.30x"], ["1.4", "1.40x"], ["1.5", "1.50x"], ["1.6", "1.60x"]]
        .forEach(([v, l]) => ssel.appendChild(new Option(l, v)));
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
      // redo section (voice + model + new take), visually subordinate
      const redo = el("div", "sa-redo");
      redo.appendChild(el("div", "sa-lbl", "Not happy? Redo with a different voice"));
      const redoRow = el("div", "sa-redo-row");
      const vsel = el("select"); OPT.tts_voice.forEach(o => vsel.appendChild(new Option(o.label, o.value)));
      vsel.value = S.values.tts_voice || vsel.value;
      const msel = el("select"); OPT.tts_model.forEach(o => msel.appendChild(new Option(o.label, o.value)));
      redoRow.appendChild(vsel); redoRow.appendChild(msel);
      redoRow.appendChild(btn("↻ " + T.new_take, async (ev) => {
        const b = ev.currentTarget; b.disabled = true;
        const body = new URLSearchParams({ speaker_name: S.values.speaker_name || "Narrator",
          tts_voice: vsel.value, tts_model: msel.value });
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
      redo.appendChild(redoRow); c.appendChild(redo);
      scrollDown();
    } else if (d.status !== "awaiting_approval" && sp.dataset.done) {
      sp.innerHTML = ""; delete sp.dataset.done;
    }
  }
  // assigned footage only (clean grid; the full grouped media wall stays on ?legacy_ui=1)
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
        if (d.open_timeline && d.project_slug) {
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
  jget("/jobs-list").then(dd => {
    const j = (dd.jobs || []).find(x => x.id === S.jobId);
    if (j && j.project_slug)
      side.appendChild(linkBtn("🎞 " + T.open_timeline, "/timeline?slug=" + encodeURIComponent(j.project_slug)));
  }).catch(() => {});
  side.appendChild(btn(T.new_project, resetToMode, "ghost"));
  side.appendChild(btn(T.nav_assets, () => showAssets(false), "ghost"));
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
    b.innerHTML = `${projThumb(p, "th")}
      <span class="meta"><b>${esc(p.title)}</b><span>${esc(fmtDate(p.edited))}</span></span>
      <span class="st ${p.failed ? "fail" : "ok"}">${p.failed ? "failed" : "ok"}</span>`;
    b.addEventListener("click", () => { close(); loadProject(p.slug); });
    list.appendChild(b);
  });
  if (!(d.projects || []).length) m.appendChild(el("div", "card-note", esc(T.no_projects)));
}

function showAssets(showHidden) {
  showLoading(T.nav_assets + "...");
  S.view = "assets"; S.showHidden = !!showHidden; renderAll(); persist();
}
let assetsProjects = [];
async function renderAssetsView() {
  const head = el("div", "assets-head");
  head.appendChild(el("h2", "", esc(T.nav_assets)));
  // Search lives here now (moved out of the sidebar): filters this project & asset library.
  const search = el("input", "assets-search");
  search.type = "search"; search.placeholder = T.search_projects;
  search.setAttribute("aria-label", T.search_projects);
  head.appendChild(search);
  const back = btn(prototypeMode ? "Back to launchpad" : ("← " + T.new_chat),
    () => { S.view = "chat"; renderAll(); persist(); }, "ghost small");
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
  if (p.failed) act.appendChild(btn("▶ " + T.continue, () => continueProject(p.slug), "primary small"));
  else if (p.results_url) act.appendChild(linkBtn("▶ " + T.check_results, p.results_url, "small"));
  // Opening a finished project goes STRAIGHT into the timeline editor (its render exists);
  // only projects without a render fall back to the project overview.
  if (p.has_timeline) act.appendChild(btn("🎞 " + (T.open || "Open"), () => openTimelineWithLoading(p.slug, p.title), "small"));
  else act.appendChild(btn("↺ " + (T.open || "Open"), () => loadProject(p.slug), "small"));
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
      b.addEventListener("click", () => { startJob(j.id); closeDrawer(); });
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
        ${p.failed ? '<span class="st fail">!</span>' : ""}`;
      b.title = p.title;
      row.classList.toggle("pinned", pins.has(p.slug));
      row.addEventListener("contextmenu", e => openProjectContextMenu(e, p));
      // finished project (has a render) -> jump straight into the timeline editor; everything
      // the old project card offered lives there. Unfinished projects keep the chat flow.
      b.addEventListener("click", () => {
        if (p.has_timeline) { openTimelineWithLoading(p.slug, p.title); return; }
        loadProject(p.slug); closeDrawer();
      });
      row.appendChild(b); box.appendChild(row);
    });
}
const CONNS = [
  ["tiktok", "TikTok", "/tiktok-status", "/tiktok-login"],
  ["x", "X", "/twitter-status", "/twitter-login"],
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
  (options || []).forEach(o => s.appendChild(new Option(o.label, o.value)));
  if (value && [...s.options].some(o => o.value === value)) s.value = value;
  else if (!value) { const d = (options || []).find(o => o.selected); if (d) s.value = d.value; }
  onChange(s.value);
  s.addEventListener("change", () => { onChange(s.value); persist(); });
  f.appendChild(s);
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
  // Light mode removed for now - always dark, no toggle.
  applyTheme("dark");
  const savedPrototype = (() => {
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
  if (init.view === "assets") { S.view = "assets"; }
  if (init.flow) { S.flow = init.flow; S.step = stepsFor(init.flow)[0]; S.completed = []; S.view = "chat"; S.jobId = null; }
  if (init.job) { S.jobId = init.job; S.jobStatus = "running"; S.view = "chat"; if (!S.flow) S.flow = "script"; S.step = "review"; S.completed = stepsFor("script").slice(0, -1); }
  if (init.project) { await loadProject(init.project); return; }
  renderAll();
}
boot();
