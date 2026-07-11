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
  card.appendChild(selectField("Reasoning mode", rm.options, rm.value, v => holder.reasoning_mode = v));
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
  const kind = p.failed ? "failed" : (p.kind || p.preview_kind || "");
  const txt = kind === "failed" ? "failed" : kind === "sfx" ? "SFX" : kind === "vfx" ? "VFX" : "";
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
  if (!v.sfx_amount) v.sfx_amount = "medium";
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

function persist() {
  clearTimeout(saveTimer);
  saveTimer = setTimeout(() => {
    const copy = { ...S };
    jpost("/chat-state", copy).catch(() => {});
  }, 350);
}

/* ------------------------------------------------------------------ flow definitions */
const FLOW_STEPS = {
  script: ["script", "hook", "source", "reasoning", "voice", "outputs", "review"],
  viraltrans: ["topic", "review"],
  reddit: ["discover", "pick"],
  longform: ["script", "settings"],
  sfx: ["upload", "settings"],
  visual: ["upload", "settings"],
  captions: ["upload", "settings"],
  project: ["summary"],
};
function stepsFor(flow) { return FLOW_STEPS[flow] || []; }
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
  renderTopbar();
  if (S.view === "assets") { renderAssetsView(); setComposer("off"); scrollDown(); return; }
  if (!S.flow) {
    // an active job renders even without a flow (e.g. reattached via deep link)
    if (S.jobId) { renderJobSection(); scrollDown(); return; }
    renderModeMenu(); setComposer("off"); scrollDown(); return;
  }

  ({ script: renderScriptFlow, viraltrans: renderViralFlow, reddit: renderRedditFlow,
     longform: renderLongformFlow, sfx: () => renderMasterFlow("sfx"),
     visual: () => renderMasterFlow("visual"), captions: () => renderMasterFlow("captions"),
     project: renderProjectFlow }[S.flow] || renderModeMenu)();

  if (S.jobId) renderJobSection();
  scrollDown();
}

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
    MODES.filter(m => m.grp === g).forEach(m => {
      const b = el("button", "mode-card");
      b.innerHTML = `<span class="mh"><span class="mi">${m.ico}</span>${esc(m.t)}</span><span class="md">${esc(m.d)}</span>`;
      b.addEventListener("click", () => selectMode(m.id));
      grid.appendChild(b);
    });
    c.appendChild(grid);
  });
}
function selectMode(id) {
  S.flow = id; S.completed = [];
  S.step = stepsFor(id)[0];
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
    const c = card();
    c.appendChild(el("div", "card-cap", "Script"));
    const ta = el("textarea", "script-box"); ta.id = "script-edit";
    ta.placeholder = T.script_placeholder; ta.value = S.values.script || "";
    c.appendChild(ta);
    const foot = el("div", "card-foot");
    foot.appendChild(btn(T.upload_txt, () => pickFile(".txt,text/plain", f => {
      f.text().then(txt => { ta.value = txt; });
    }), "ghost"));
    foot.appendChild(btn(T.load_project, openProjectPicker, "ghost"));
    foot.appendChild(el("span", "spacer"));
    foot.appendChild(btn(T.continue, () => {
      const v = ta.value.trim();
      if (!v) { ta.focus(); return; }
      S.values.script = v;
      if (S.values.hook_text && !v.includes(S.values.hook_text)) S.values.hook_text = "";
      completeStep("script", "hook");
    }, "primary"));
    c.appendChild(foot);
    setComposer("off");
    return;
  }

  // step: hook
  if (done("script")) {
    msgA(esc(T.mark_hook_q));
    if (done("hook")) {
      msgU(S.values.hook_text
        ? `${esc(T.hook_marked)}: <i>${esc(S.values.hook_text.slice(0, 90))}</i>` : esc(T.no_hook), "hook");
    } else if (S.step === "hook") {
      renderHookCard(); setComposer("off"); return;
    }
  }

  // step: visual source
  if (done("hook")) {
    msgA(esc(T.visual_source_q));
    if (done("source")) {
      msgU(S.values.clip_source === "scrape"
        ? `${esc(T.src_scrape)} · ${esc(S.values.scraping_engine === "v1" ? T.engine_v1 : T.engine_v2)}`
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
        c.appendChild(selectField("Reasoning mode", rm.options, rm.value, v => S.values.reasoning_mode = v));
        c.appendChild(el("div", "hint", "Higher reasoning can improve difficult tasks but may increase response time and cost."));
      }
      const foot = el("div", "card-foot");
      foot.appendChild(btn(T.back, () => editStep("source"), "ghost"));
      foot.appendChild(btn("Continue", () => completeStep("reasoning", "voice"), "primary"));
      c.appendChild(foot);
      setComposer("off"); return;
    }
  }

  // step: voice + speaker
  if (done("reasoning")) {
    msgA(esc(T.voice_q));
    if (done("voice")) {
      msgU(`${esc(S.values.tts_voice)} · ${esc(labelFor(OPT.tts_model, S.values.tts_model))}` +
        (S.values.enable_speaker_hook ? " · " + esc(T.speaker_video) : ""), "voice");
    } else if (S.step === "voice") {
      renderVoiceCard(); setComposer("off"); return;
    }
  }

  // step: outputs
  if (done("voice")) {
    msgA(esc(T.outputs_q));
    if (done("outputs")) {
      msgU(esc(outputsSummary()), "outputs");
    } else if (S.step === "outputs") {
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
  foot.appendChild(btn(T.back, () => editStep("script"), "ghost"));
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
  const seg = el("div", "choices");
  [["generate", T.src_generate, T.src_generate_d], ["scrape", T.src_scrape, T.src_scrape_d]].forEach(([v, t, d]) => {
    const b = el("button", "choice" + (S.values.clip_source === v ? " sel" : ""),
      `${esc(t)}<small>${esc(d)}</small>`);
    b.addEventListener("click", () => { S.values.clip_source = v; renderAll(); persist(); });
    seg.appendChild(b);
  });
  c.appendChild(seg);
  const wrap = el("div"); wrap.style.marginTop = "12px"; c.appendChild(wrap);

  if (S.values.clip_source === "generate") {
    wrap.appendChild(selectField(T.video_model, OPT.video_model, S.values.video_model,
      v => S.values.video_model = v));
    wrap.appendChild(selectField(T.image_model, OPT.image_model, S.values.image_model,
      v => S.values.image_model = v));
  } else {
    // engine
    wrap.appendChild(el("div", "card-cap", esc(T.scrape_engine)));
    const engs = el("div", "choices");
    [["v2", T.engine_v2], ["v1", T.engine_v1]].forEach(([v, t]) => {
      const b = el("button", "choice" + ((S.values.scraping_engine || "v2") === v ? " sel" : ""), esc(t));
      b.addEventListener("click", () => { S.values.scraping_engine = v; renderAll(); persist(); });
      engs.appendChild(b);
    });
    wrap.appendChild(engs);
    // relevancy
    wrap.appendChild(el("div", "card-cap", esc(T.script_relevancy)));
    const rr = el("div", "range-row");
    const rg = el("input"); rg.type = "range"; rg.min = 0; rg.max = 100; rg.step = 5;
    rg.value = S.values.script_relevancy || "70";
    const rv = el("span", "range-val", (S.values.script_relevancy || "70") + "%");
    rg.addEventListener("input", () => { S.values.script_relevancy = rg.value; rv.textContent = rg.value + "%"; persist(); });
    rr.appendChild(rg); rr.appendChild(rv); wrap.appendChild(rr);
    wrap.appendChild(selectField(T.scrape_sort || "Sort results by", OPT.scrape_sort || [
      {value:"RELEVANCE",label:"Relevance (best for on-topic clips)"},
      {value:"MOST_LIKED",label:"Most liked"}, {value:"MOST_VIEWED",label:"Most viewed"},
      {value:"MOST_RECENT",label:"Most recent"}
    ], S.values.scrape_sort || "RELEVANCE", v => S.values.scrape_sort = v));
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
    // connections
    wrap.appendChild(el("div", "card-cap", esc(T.connections)));
    wrap.appendChild(connectionRow("tiktok", T.connect_tiktok, "/tiktok-status", "/tiktok-login"));
    wrap.appendChild(connectionRow("x", T.connect_x, "/twitter-status", "/twitter-login"));
    // background music
    wrap.appendChild(el("div", "card-cap", esc(T.background_music)));
    const mrow = el("div", "chip-add");
    const msel = el("select");
    msel.appendChild(new Option(T.bgm_none, "none"));
    const play = btn("▶ " + T.preview, () => {
      const url = msel.selectedOptions[0] && msel.selectedOptions[0].dataset.url;
      if (url) { const a = ensureAudio(); a.src = url; a.play().catch(() => {}); }
    }, "ghost");
    jget("/music-list").then(d => {
      (d.tracks || []).forEach(t => {
        const o = new Option(t.name, t.file); o.dataset.url = t.url; msel.appendChild(o);
      });
      msel.value = S.values.background_music_choice || "none";
    }).catch(() => {});
    msel.addEventListener("change", () => {
      S.values.background_music_choice = msel.value;
      S.values.background_music_enabled = msel.value !== "none";
      S.values.out_background_music = msel.value !== "none" ? S.values.out_background_music : S.values.out_background_music;
      persist();
    });
    mrow.appendChild(msel); mrow.appendChild(play); wrap.appendChild(mrow);
  }

  const foot = el("div", "card-foot");
  foot.appendChild(btn(T.back, () => editStep("hook"), "ghost"));
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
  const row = el("div", "fld-row");
  row.appendChild(selectField(T.tts_voice, OPT.tts_voice, S.values.tts_voice, v => S.values.tts_voice = v));
  row.appendChild(selectField(T.tts_model, OPT.tts_model, S.values.tts_model, v => S.values.tts_model = v));
  c.appendChild(row);
  const prev = btn("▶ " + T.preview, () => {
    const a = ensureAudio(); a.src = BOOT.voices_preview + encodeURIComponent(S.values.tts_voice || "");
    a.play().catch(() => {});
  }, "ghost small");
  c.appendChild(prev);
  c.appendChild(toggleField(T.fresh_take, S.values.force_regenerate, v => S.values.force_regenerate = v));
  c.appendChild(toggleField(T.speaker_video, S.values.enable_speaker_hook, v => {
    S.values.enable_speaker_hook = v; renderAll(); persist();
  }));
  if (S.values.enable_speaker_hook) {
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
  foot.appendChild(btn(T.back, () => editStep("reasoning"), "ghost"));
  foot.appendChild(el("span", "spacer"));
  foot.appendChild(btn(T.continue, () => completeStep("voice", "outputs"), "primary"));
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
  ["out_background_music", "Background music"], ["out_captions", "Captions"],
];
function renderOutputsCard() {
  const c = card();
  const grid = el("div", "tgl-grid");
  const scrape = S.values.clip_source === "scrape";
  OUTPUT_FIELDS.forEach(([k, label]) => {
    const dis = scrape && ["out_web_images", "out_wikimedia", "out_gpt_images"].includes(k);
    const t = toggleField(label, S.values[k], v => S.values[k] = v, dis);
    grid.appendChild(t);
  });
  c.appendChild(grid);
  c.appendChild(el("div", "card-cap", "Run"));
  c.appendChild(toggleField(T.halt_after_speech, S.values.halt_after_speech, v => S.values.halt_after_speech = v));
  c.appendChild(selectField(T.sfx_amount, OPT.sfx_amount, S.values.sfx_amount, v => S.values.sfx_amount = v));
  const foot = el("div", "card-foot");
  foot.appendChild(btn(T.back, () => editStep("voice"), "ghost"));
  foot.appendChild(btn(T.presets, openPresets, "ghost"));
  foot.appendChild(el("span", "spacer"));
  foot.appendChild(btn(T.continue, () => completeStep("outputs", "review"), "primary"));
  c.appendChild(foot);
}
function outputsSummary() {
  const on = OUTPUT_FIELDS.filter(([k]) => S.values[k]).map(([, l]) => l);
  let s = on.join(", ") || "No outputs";
  if (S.values.halt_after_speech) s += " · " + T.halt_after_speech;
  s += " · " + T.sfx_amount + ": " + (S.values.sfx_amount || "medium");
  return s;
}

function renderReviewCard() {
  const c = card();
  c.appendChild(el("h3", "", esc(T.ready_create)));
  const g = el("div", "sum-grid");
  const rows = [
    ["Mode", T.mode_script_t, null],
    ["Visual source", S.values.clip_source === "scrape" ? T.src_scrape : T.src_generate, "source"],
  ];
  if (S.values.clip_source === "scrape") {
    rows.push([T.scrape_engine, S.values.scraping_engine === "v1" ? "Scrape V1" : "Scrape V2", "source"]);
    rows.push([T.script_relevancy, (S.values.script_relevancy || "70") + "%", "source"]);
    if (S.values.scrape_terms) rows.push(["Search terms", S.values.scrape_terms, "source"]);
  } else {
    rows.push([T.video_model, labelFor(OPT.video_model, S.values.video_model), "source"]);
    rows.push([T.image_model, labelFor(OPT.image_model, S.values.image_model), "source"]);
  }
  rows.push(["Reasoning model", labelFor(OPT.reasoning_model, S.values.reasoning_model), "reasoning"]);
  rows.push(["Voice", S.values.tts_voice + " · " + labelFor(OPT.tts_model, S.values.tts_model), "voice"]);
  rows.push(["Speaker video", S.values.enable_speaker_hook ? "On" : "Off", "voice"]);
  rows.push(["Hook", S.values.hook_text ? T.hook_marked : T.no_hook, "hook"]);
  if (S.values.impact_word) rows.push(["Impact word", S.values.impact_word, "hook"]);
  rows.push([T.background_music, (S.values.background_music_choice && S.values.background_music_choice !== "none")
      ? S.values.background_music_choice : "Off", "source"]);
  rows.push([T.sfx_amount, S.values.sfx_amount || "medium", "outputs"]);
  rows.forEach(([k, v, editKey]) => {
    g.appendChild(el("span", "k", esc(k)));
    g.appendChild(el("span", "v", esc(v)));
    const e = el("span");
    if (editKey) { const b = el("button", "edit-link", T.edit); b.addEventListener("click", () => editStep(editKey)); e.appendChild(b); }
    g.appendChild(e);
  });
  c.appendChild(g);
  c.appendChild(el("div", "card-cap", "Outputs"));
  const ul = el("ul", "sum-list");
  OUTPUT_FIELDS.filter(([k]) => S.values[k]).forEach(([, l]) => ul.appendChild(el("li", "", esc(l))));
  c.appendChild(ul);
  if (S.projectSlug) {
    c.appendChild(el("div", "card-note", esc(T.project_loaded) + ": " + esc(S.projectTitle || S.projectSlug)
      + " · " + esc(labelForRunMode(S.values.loaded_project_mode))));
  }
  const foot = el("div", "card-foot");
  foot.appendChild(btn(T.back, () => editStep("outputs"), "ghost"));
  foot.appendChild(btn(T.presets, openPresets, "ghost"));
  foot.appendChild(el("span", "spacer"));
  foot.appendChild(btn("⚡ " + T.create_short, submitRun, "primary"));
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
    const drop = el("div", "upl-drop", "📎 " + esc(T.attach_video) + "<br><small>MP4 · MOV · WebM</small>");
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
      c.appendChild(selectField(T.sfx_amount, OPT.master_sfx_amount, S.master.sfx_amount || "medium",
        v => S.master.sfx_amount = v));
    } else if (kind === "visual") {
      c.appendChild(selectField(T.analysis_agent, OPT.vfx_reasoning, S.master.reasoning_model,
        v => { const changed=!!S.master.reasoning_model&&S.master.reasoning_model!==v; S.master.reasoning_model = v; S.master.reasoning_mode = reasoningOptions(v, S.master.reasoning_mode).value; if(changed)setTimeout(renderAll,0); }));
      appendReasoningField(c, S.master.reasoning_model || firstVal(OPT.vfx_reasoning), S.master);
      c.appendChild(selectField(T.effect_amount, OPT.vfx_amount, S.master.vfx_amount || "medium",
        v => S.master.vfx_amount = v));
      c.appendChild(toggleField(T.neko_toggle, S.master.add_characters !== false,
        v => S.master.add_characters = v));
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
    S.completed = ["script", "hook", "source", "reasoning", "voice", "outputs"];
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

function renderJobSection() {
  typedMsg(chat, T.project_started);
  // #111 - assigned footage grid (ONLY the clips chosen for scenes) sits at the TOP
  const mwrap = el("div"); mwrap.id = "job-media"; chat.appendChild(mwrap);
  const scrapeView=el("div","scrape-browser-card"); scrapeView.id="scrape-browser-card"; scrapeView.hidden=true;
  scrapeView.innerHTML='<div class="scrape-browser-head"><b>Live scrape browser</b><span id="scrape-browser-meta"></span></div><img id="scrape-browser-image" alt="Current TikTok or X scraper page">';
  chat.appendChild(scrapeView);
  // technical console (terminal-styled, collapsible)
  const tcard = card("term-card"); tcard.id = "job-tech-card";
  const thead = el("div", "term-head");
  const tbtn = el("button", "term-toggle");
  tbtn.innerHTML = `<span class="term-dot"></span><span class="term-dot"></span><span class="term-dot"></span>
    <span class="term-title">${esc(T.show_tech)}</span><span class="term-chev">▾</span>`;
  tbtn.addEventListener("click", () => {
    const lg = $("job-log");
    const show = lg.hasAttribute("hidden");
    if (show) { lg.removeAttribute("hidden"); tcard.classList.add("open"); lg.scrollTop = lg.scrollHeight; }
    else { lg.setAttribute("hidden", ""); tcard.classList.remove("open"); }
    tbtn.querySelector(".term-title").textContent = show ? T.hide_tech : T.show_tech;
  });
  thead.appendChild(tbtn); tcard.appendChild(thead);
  const lg = el("pre", "tech-log"); lg.id = "job-log"; lg.setAttribute("hidden", "");
  tcard.appendChild(lg);
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
  const badge = el("span", "tb-badge run", esc(S.jobStatus || "running")); badge.id = "job-badge";
  head.appendChild(badge);
  const cancelB = btn(T.cancel_process, cancelJob, "danger small"); cancelB.id = "job-cancel";
  head.appendChild(cancelB);
  c.appendChild(head);
  const prog = el("div", "prog-embed"); prog.id = "job-progress"; c.appendChild(prog);
  // outputs / result container (appears below the box when the run completes)
  const owrap = el("div"); owrap.id = "job-out"; chat.appendChild(owrap);
  pollJob(); pollTimer = setInterval(pollJob, 2500);
  pollScrapeBrowser(); scrapePreviewTimer=setInterval(pollScrapeBrowser,3000);
  setComposer("off");
}
function stopPolling() { if (pollTimer) { clearInterval(pollTimer); pollTimer = null; } if(scrapePreviewTimer){clearInterval(scrapePreviewTimer);scrapePreviewTimer=null;} }

async function pollScrapeBrowser(){
  const card=$("scrape-browser-card"), image=$("scrape-browser-image"), meta=$("scrape-browser-meta");
  if(!card||!image)return;
  try{
    const d=await jget('/scrape-browser-status');
    if(!d.available){card.hidden=true;return;}
    card.hidden=false;
    meta.textContent=[d.platform,d.query,d.sort].filter(Boolean).join(' · ');
    if(String(image.dataset.version||'')!==String(d.version)){
      image.dataset.version=String(d.version); image.src='/scrape-browser-preview?v='+encodeURIComponent(d.version);
    }
  }catch(e){card.hidden=true;}
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
      if (evts) typedMsg(evts, text);
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
  typedMsg(mw, `Assigned footage — ${items.length} clip${items.length === 1 ? "" : "s"} chosen for your scenes.`);
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
    const badge = $("job-badge");
    if (badge) { badge.textContent = d.status; badge.className = "tb-badge" + (d.status === "running" ? " run" : ""); }
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
  // speech approval
  const sp = $("job-speech");
  if (sp) {
    if (d.status === "awaiting_approval" && d.speech_audio_url && !sp.dataset.done) {
      sp.dataset.done = "1"; sp.innerHTML = "";
      const m = el("div", "msg assistant"); m.appendChild(el("div", "bubble", esc(T.voiceover_ready))); sp.appendChild(m);
      const c = el("div", "chat-card"); sp.appendChild(c);
      const au = el("audio", "inline-audio"); au.controls = true; au.src = d.speech_audio_url; c.appendChild(au);
      const foot = el("div", "card-foot");
      // narration speed: the preview is already baked at `curSpeed`; picking a different one
      // previews it live (audio playbackRate = chosen/current) and re-tempos the voice on approve.
      const curSpeed = +(d.speech_speed || 0) || 1.15;
      const ssel = el("select"); ssel.title = "Narration speed before the run continues";
      [["", "Keep current speed (" + curSpeed.toFixed(2) + "x)"],
       ["1.0", "1.00x"], ["1.15", "1.15x"], ["1.2", "1.20x"],
       ["1.3", "1.30x"], ["1.4", "1.40x"], ["1.5", "1.50x"], ["1.6", "1.60x"]]
        .forEach(([v, l]) => ssel.appendChild(new Option(l, v)));
      // live preview of the selected speed relative to the baked one
      ssel.addEventListener("change", () => {
        const sel = +ssel.value || curSpeed;
        au.playbackRate = Math.max(0.5, Math.min(2.5, sel / curSpeed));
        try { au.currentTime = 0; au.play().catch(() => {}); } catch (e) {}
      });
      foot.appendChild(ssel);
      foot.appendChild(btn("✓ " + T.approve_continue, async () => {
        const q = ssel.value ? "&speed=" + encodeURIComponent(ssel.value) : "";
        await fetch("/approve-speech?id=" + encodeURIComponent(S.jobId) + q, { method: "POST" });
        sp.innerHTML = ""; delete sp.dataset.done;
      }, "primary"));
      const vsel = el("select"); OPT.tts_voice.forEach(o => vsel.appendChild(new Option(o.label, o.value)));
      vsel.value = S.values.tts_voice || vsel.value;
      const msel = el("select"); OPT.tts_model.forEach(o => msel.appendChild(new Option(o.label, o.value)));
      foot.appendChild(vsel); foot.appendChild(msel);
      foot.appendChild(btn("↻ " + T.new_take, async (ev) => {
        const b = ev.currentTarget; b.disabled = true;
        const body = new URLSearchParams({ speaker_name: S.values.speaker_name || "Narrator",
          tts_voice: vsel.value, tts_model: msel.value });
        // #127 - /replace-speech regenerates the voiceover as a follow-on run in the SAME project.
        // Follow that new job in-place (same chat) instead of leaving the poller on the old job,
        // which used to surface a spurious "run cancelled" + "new chat".
        try {
          const r = await fetch("/replace-speech?id=" + encodeURIComponent(S.jobId), { method: "POST",
            headers: { "Content-Type": "application/x-www-form-urlencoded" }, body });
          const jid = new URL(r.url, location.href).searchParams.get("id");
          if (jid) { S.jobId = jid; S.jobStatus = "running"; persist(); }
        } catch (e) {}
        sp.innerHTML = ""; delete sp.dataset.done;
        renderTopbar();
      }, "danger"));
      c.appendChild(foot);
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
  if (["done", "error", "cancelled"].includes(d.status)) {
    stopPolling();
    const cancelB = $("job-cancel"); if (cancelB) cancelB.remove();
    if (!$("job-final")) {
      const fin = el("div"); fin.id = "job-final"; chat.appendChild(fin);
      if (d.status === "done") {
        const m = el("div", "msg assistant"); m.appendChild(el("div", "bubble", "✓ " + esc(T.your_short_ready))); fin.appendChild(m);
        renderResultCard(fin, d);
      } else if (d.status === "error") {
        const c = el("div", "chat-card err-card"); fin.appendChild(c);
        c.appendChild(el("h3", "", esc(T.job_error)));
        if (d.error_html) c.appendChild(el("div", "embed", d.error_html));
        const foot = el("div", "card-foot");
        foot.appendChild(btn(T.continue_project, () => S.projectSlug ? continueProject(S.projectSlug) : resetToMode(), "small"));
        foot.appendChild(btn(T.new_chat, resetToMode, "ghost small"));
        c.appendChild(foot);
      } else {
        const m = el("div", "msg assistant"); m.appendChild(el("div", "bubble", esc(T.job_cancelled))); fin.appendChild(m);
        const c = el("div", "chat-card"); fin.appendChild(c);
        const foot = el("div", "card-foot");
        foot.appendChild(btn(T.new_chat, resetToMode, "small"));
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
  const back = btn("← " + T.new_chat, () => { S.view = "chat"; renderAll(); persist(); }, "ghost small");
  head.appendChild(back);
  chat.appendChild(head);
  const grid = el("div", "assets-grid"); chat.appendChild(grid);
  const empty = el("div", "card-note", esc(T.no_projects)); empty.hidden = true; chat.appendChild(empty);
  const footer = el("div", "card-foot"); chat.appendChild(footer);
  const d = await jget("/projects-list" + (S.showHidden ? "?hidden=1" : ""));
  hideLoading();
  assetsProjects = d.projects || [];
  const paint = () => {
    const q = (search.value || "").trim().toLowerCase();
    grid.innerHTML = "";
    const rows = assetsProjects.filter(p => !q ||
      (p.title || "").toLowerCase().includes(q) || (p.slug || "").toLowerCase().includes(q));
    rows.forEach(p => grid.appendChild(assetCard(p)));
    empty.hidden = rows.length > 0;
  };
  search.addEventListener("input", paint);
  paint();
  if (S.showHidden) footer.appendChild(btn("← " + T.hide_hidden, () => showAssets(false), "ghost"));
  else if (d.hidden_count) footer.appendChild(btn(`👁 ${T.show_hidden} (${d.hidden_count})`, () => showAssets(true), "ghost"));
  setTimeout(() => { try { search.focus(); } catch (e) {} }, 30);
}
function assetCard(p) {
  const c = el("div", "asset-card2");
  const previewKind=p.failed?'failed':(p.kind||p.preview_kind||'');
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
  act.appendChild(btn("↺ Open", () => loadProject(p.slug), "small"));
  if (p.has_timeline) act.appendChild(linkBtn("🎞 Edit", "/timeline?slug=" + encodeURIComponent(p.slug), "small"));
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
  ["timeline", "🎞", T.nav_timeline, openTimelineNav],
  ["sfx", "🔊", T.nav_sfx, () => { resetToMode(); selectMode("sfx"); }],
  ["vfx", "➜", T.nav_vfx, () => { resetToMode(); selectMode("visual"); }],
  ["captions", "💬", T.nav_captions, () => { resetToMode(); selectMode("captions"); }],
];
function renderNav() {
  const nav = $("sb-nav"); nav.innerHTML = "";
  NAV.forEach(([id, ico, label, fn]) => {
    const b = el("button");
    b.innerHTML = `<span class="ico">${ico}</span>${esc(label)}`;
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
      showLoading(T.open_timeline + "...");
      location.href = "/timeline?slug=" + encodeURIComponent(S.projectSlug); return;
    }
    alert(T.no_timeline_yet); return;
  }
  // project picker filtered to timeline-capable projects
  const d = await jget("/projects-list");
  const withTl = (d.projects || []).filter(p => p.has_timeline);
  if (!withTl.length) { alert(T.no_timeline_yet); return; }
  const scrim = el("div", "modal-scrim"); document.body.appendChild(scrim);
  scrim.addEventListener("click", e => { if (e.target === scrim) scrim.remove(); });
  const m = el("div", "modal"); scrim.appendChild(m);
  m.appendChild(el("h3", "", esc(T.pick_project)));
  const list = el("div", "list"); m.appendChild(list);
  withTl.forEach(p => {
    const b = el("button", "sb-proj");
    b.innerHTML = `${projThumb(p, "th")}<span class="meta"><b>${esc(p.title)}</b><span>${esc(fmtDate(p.edited))}</span></span>`;
    b.addEventListener("click", () => {
      showLoading(T.open_timeline + "...");
      location.href = "/timeline?slug=" + encodeURIComponent(p.slug);
    });
    list.appendChild(b);
  });
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
      const b = el("button", "sb-job");
      b.innerHTML = `<b>${esc(j.project_slug || j.kind)}</b><span class="${j.status === "running" ? "run" : ""}">${esc(j.status)} · ${esc(j.last_log || "")}</span>`;
      b.addEventListener("click", () => { startJob(j.id); closeDrawer(); });
      box.appendChild(b);
    });
  } catch (e) {}
  paintConnections();
}
function paintSidebarProjects() {
  const box = $("sb-projects");
  // Search moved to the Projects & Assets tab; the sidebar just lists the recent projects.
  box.innerHTML = "";
  sidebarProjects
    .slice(0, 24)
    .forEach(p => {
      const row = el("div", "sb-proj-row");
      const b = el("button", "sb-proj");
      b.innerHTML = `${projThumb(p, "th")}
        <span class="meta"><b>${esc(p.title)}</b><span>${esc(fmtDate(p.edited))}</span></span>
        ${p.failed ? '<span class="st fail">!</span>' : ""}`;
      b.title = p.title;
      b.addEventListener("click", () => { loadProject(p.slug); closeDrawer(); });
      const hide = el("button", "sb-proj-hide", "&times;");
      hide.title = T.hide; hide.setAttribute("aria-label", `${T.hide}: ${p.title}`);
      hide.addEventListener("click", async e => {
        e.stopPropagation(); hide.disabled = true;
        await jpost("/hide-project", {slug:p.slug, hidden:true});
        sidebarProjects = sidebarProjects.filter(item => item.slug !== p.slug);
        paintSidebarProjects();
      });
      row.appendChild(b); row.appendChild(hide); box.appendChild(row);
    });
}
const CONNS = [
  ["tiktok", "TikTok", "/tiktok-status", "/tiktok-login"],
  ["x", "X", "/twitter-status", "/twitter-login"],
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
function connectionRow(key, label, statusUrl, loginUrl) {
  const row = el("div", "sb-conn");
  const dot = el("span", "dot"); row.appendChild(dot);
  const txt = el("span", "", esc(label)); row.appendChild(txt);
  const b = el("button", "", "🔗"); row.appendChild(b);
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
function btn(label, fn, cls) {
  const b = el("button", "btn " + (cls || ""), label);
  b.addEventListener("click", fn);
  return b;
}
function linkBtn(label, href, cls) {
  const a = el("a", "btn " + (cls || ""), label); a.href = href;
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
  try { localStorage.setItem("sl-chat-theme", t); } catch (e) {}
}
function wireChrome() {
  const saved = (() => { try { return localStorage.getItem("sl-chat-theme"); } catch (e) { return null; } })();
  applyTheme(saved || "dark");
  $("theme-toggle").addEventListener("click", () => {
    applyTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark");
  });
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
