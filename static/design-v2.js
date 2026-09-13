/* ShortsLab Design V2
   Presentation-only enhancement layer. It does not submit data, call production routes,
   or alter the flow state machine. Add ?design=v1 to any shell URL for the preserved UI. */
(function () {
  "use strict";
  const root = document.documentElement;
  if (!root.classList.contains("design-v2")) return;

  const icon = (name) => {
    const paths = {
      link: '<path d="M10 13a5 5 0 0 0 7.5.5l2-2a5 5 0 0 0-7-7l-1.1 1.1"/><path d="M14 11a5 5 0 0 0-7.5-.5l-2 2a5 5 0 0 0 7 7l1.1-1.1"/>',
      moon: '<path d="M20.5 14.2A8 8 0 0 1 9.8 3.5 8.6 8.6 0 1 0 20.5 14.2Z"/>',
      sun: '<circle cx="12" cy="12" r="3.5"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>',
      chevron: '<path d="m9 18 6-6-6-6"/>',
    };
    return `<svg class="v2-inline-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${paths[name] || paths.chevron}</svg>`;
  };

  function syncThemeControl() {
    const input = document.getElementById("theme-toggle");
    const label = document.getElementById("theme-toggle-label");
    const host = document.querySelector(".sb-theme-switch");
    if (!input || !host) return;
    const dark = root.dataset.theme !== "light";
    input.checked = dark;
    if (label) label.textContent = dark ? "Dark" : "Light";
    host.dataset.mode = dark ? "dark" : "light";
    host.style.setProperty("--v2-theme-icon", dark ? '"moon"' : '"sun"');
    let glyph = host.querySelector(".sb-theme-glyph");
    if (!glyph) {
      glyph = document.createElement("span");
      glyph.className = "sb-theme-glyph";
      host.insertBefore(glyph, host.firstChild);
    }
    const glyphName = dark ? "moon" : "sun";
    if (glyph.dataset.icon !== glyphName) {
      glyph.dataset.icon = glyphName;
      glyph.innerHTML = icon(glyphName);
    }
  }

  function addVersionBadge() {
    const foot = document.querySelector(".sb-foot");
    if (!foot || foot.querySelector(".v2-version-badge")) return;
    const badge = document.createElement("span");
    badge.className = "v2-version-badge";
    badge.textContent = "DESIGN V2";
    badge.title = "Add ?design=v1 to the URL to open the preserved interface";
    foot.appendChild(badge);
  }

  function polishConnectionButtons(scope) {
    (scope || document).querySelectorAll(".sb-conn button").forEach((button) => {
      const text = button.textContent.trim();
      if (!text.includes("🔗")) return;
      const label = text.replace("🔗", "").trim() || "Connect";
      button.innerHTML = icon("link") + `<span>${label}</span>`;
    });
  }

  function decorateLaunchpad() {
    const home = document.querySelector(".proto-launchpad");
    if (!home || home.dataset.v2Ready) return;
    home.dataset.v2Ready = "1";
    const create = home.querySelector(".proto-work-section");
    const upgrade = home.querySelector(".proto-upgrade-section");
    if (create) create.dataset.section = "create";
    if (upgrade) upgrade.dataset.section = "upgrade";
  }

  function decorateFlow() {
    const app = document.getElementById("app");
    if (!app) return;
    try {
      if (typeof S !== "undefined") {
        app.dataset.flow = S.flow || "home";
        app.dataset.step = S.step || "mode";
        if (S.projectTitle) document.title = `${S.projectTitle} · ShortsLab`;
        else document.title = S.flow ? `${String(S.flow).replace(/(^|_)(\w)/g, (_,a,b)=>" "+b.toUpperCase()).trim()} · ShortsLab` : "ShortsLab";
      }
    } catch (e) {}
    document.querySelectorAll(".proto-config-head").forEach((head) => {
      if (head.dataset.v2Ready) return;
      head.dataset.v2Ready = "1";
      const title = head.querySelector("h1");
      if (title) title.setAttribute("tabindex", "-1");
      const description = head.querySelector(".proto-config-title > p");
      if (description) description.hidden = true;
    });
    document.querySelectorAll(".card-foot").forEach((foot) => {
      foot.classList.toggle("v2-has-primary", !!foot.querySelector(".primary"));
    });
  }

  /* The home screen intentionally hides the legacy top bar.  On a narrow
     viewport the sidebar becomes an off-canvas drawer, so its recovery button
     must not stay trapped inside that hidden top bar.  Moving the existing
     control preserves the click handler and keyboard semantics from the shell. */
  function syncMobileMenuAccess() {
    const app = document.getElementById("app");
    const menu = document.getElementById("tb-menu");
    const topbar = document.getElementById("topbar");
    if (!app || !menu || !topbar) return;
    const needsFloatingMenu = app.classList.contains("proto-home")
      && (window.matchMedia("(max-width: 900px)").matches || app.classList.contains("sb-collapsed"));
    let host = document.getElementById("v2-mobile-menu-host");
    if (needsFloatingMenu) {
      if (!host) {
        host = document.createElement("div");
        host.id = "v2-mobile-menu-host";
        app.appendChild(host);
      }
      if (menu.parentElement !== host) host.appendChild(menu);
    } else {
      if (menu.parentElement !== topbar) topbar.prepend(menu);
      host?.remove();
    }
  }

  function sync() {
    addVersionBadge();
    syncThemeControl();
    polishConnectionButtons(document);
    decorateLaunchpad();
    decorateFlow();
    syncMobileMenuAccess();
  }

  const observer = new MutationObserver(() => requestAnimationFrame(sync));
  observer.observe(document.body, { childList: true, subtree: true, attributes: true, attributeFilter: ["data-theme", "class"] });
  document.getElementById("theme-toggle")?.addEventListener("change", () => requestAnimationFrame(syncThemeControl));
  root.addEventListener("transitionend", syncThemeControl);
  window.addEventListener("resize", () => requestAnimationFrame(syncMobileMenuAccess), { passive: true });
  sync();
})();
