# ShortsLab Design V2

Design V2 is a presentation-only redesign. It does not change generation,
scraping, editing, rendering, or project pipeline behavior.

## Covered surfaces

- Main shell, expanded and collapsed sidebar, navigation, recent projects,
  active jobs, connection state, theme control, and project context menus
- New Creation launchpad and every mode entry card
- Clip Short, Fact Short, AI Short, Sketch Explainer, Physics, Low Poly,
  Enhance Video, Action Edit, and master configuration flows
- Uploads, choices, sliders, toggles, form controls, review steps, and dialogs
- Projects & Assets, empty/error states, approvals, processing, live progress,
  technical details, and post-render results
- Timeline editor toolbar, stage, media library, inspector, transport,
  tracks, clips, popovers, and script modal
- Dark and light themes, keyboard focus, reduced motion, and responsive layouts

## Files

- `static/design-v2.css`: semantic tokens and visual treatment
- `static/design-v2.js`: presentation-only UI decoration and theme syncing
- `static/timeline-theme.css`: Design V2 timeline skin
- `static/chat-shell.js`: launchpad hierarchy and presentation structure
- `chat_ui.py` and `app.py`: Design V2 asset loading
- `design-system/shortslab-design-v2/MASTER.md`: design-system source of truth

## Switching back to V1

Open the app with `?design=v1`, for example:

`http://127.0.0.1:7899/?design=v1`

The original pre-V2 files are also preserved at:

`D:\data\AutoShortsClaude\backups\ui_v1_20260826_182134`

The folder includes a binary-safe working-tree patch named
`working_tree_before_design_v2.patch`.
