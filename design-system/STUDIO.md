# Shortslab Studio — design direction

The September 5 redesign replaces the default retro computer showroom with an editorial video studio. The primary experience is the studio home, direct production tools, project library and timeline editor.

## Visual system

- Graphite canvas `#111313`; raised olive-neutral surfaces `#1a1e19`.
- Primary action `#dbf3a8` with dark ink. Accent is reserved for actions, selection and brand detail.
- Primary text `#f1f0e9`, secondary `#c4c9c1`, supporting text `#a1aaa0`.
- Segoe UI / Arial for the application. Georgia italic only for the studio headline.
- Radius scale: 6px controls, 10–12px panels, 14–16px prominent containers. Timeline track coordinates stay unchanged.
- 180–250ms feedback. Reduced motion removes transitions and animation. Video preview plays only on request.

## Product structure

The home page introduces Clip Studio with a real local video preview, followed by AI cinema, Sketch stories, Physics lab and mastering. Each opens the existing production form and its original endpoint contract. No fabricated usage metrics or generated project data.

The library has text search plus All projects, With video, In edit and Needs attention filters. Project actions use the existing endpoints.

Ctrl/Cmd K opens a searchable tool palette. Arrow keys select results; Enter opens; Escape closes. Dialogs contain keyboard focus and restore it on close.

## Implementation

`static/shell-v3.js` owns studio rendering, routing, dialogs, form state and library filtering. `static/shell-v3.css` carries shared tokens and responsive styles. `static/shell-window.css` and `static/timeline-theme.css` extend the visual system to server-rendered windows. The production manifests and endpoints remain the existing ones.

## Verification log

- `node --check static/shell-v3.js`: passes.
- `python -m unittest tests.test_shell_v3_wiring tests.test_shell_v3_windows tests.test_served_js_is_valid -q`: 71 tests pass after updating the obsolete exact retro palette/radius assertions to the studio palette/scale.
- Browser checks: studio home; Clip editor at 1280 and 375px; Sketch; Physics; mastering at desktop and 375px; real project library; real timeline with loaded media.
- Library With video: 90 matching projects in the observed data; all 48 initially displayed cards had video links.
- Ctrl K, query “sketch”, Enter: opened `/longform` and removed the dialog.
- Preview, remaining responsive cases, deeper accessibility and visual refinement continue under the active user goal.

Production jobs were not launched as part of UI verification.
