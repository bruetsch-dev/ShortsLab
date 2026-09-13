# Clip Short editorial checks

New beat plans carry `editorial_role` (`context`, `action`, `payoff`), `sequence_id`,
`required_action`, and `required_result`. These survive normalization and project creation.
Context can use illustrative footage; actions and results need visible evidence for the
actual narration line. Source text is data, never instructions to the reviewer.

V4 scans the full source for cuts. Failed scans are rejected, not treated as an empty
cut list. Candidate windows come from individual shots, with at most 18 proposals per
source. Short windows remain candidates but cannot cover a beat below the 0.84 speed
floor. Selected windows never extend beyond reviewed seconds. The reviewer locates
the action within a window so shortening cannot knowingly cut off its result.

Distinct non-overlapping moments from the same source may continue a declared sequence
(maximum four uses), with a small continuity preference. Window fingerprints still
reject repeated pictures. A score earned for a different narration line cannot prove
an action for a new line. Coverage-fill remains an explicitly unfinished editor draft.

The shared renderer checks the **actual decoded range**, after prepared media and trims.
An explicit source in-point takes precedence over automatic same-source continuation.
Short edge fragments can be trimmed away with modest retiming. Larger internal cuts
trigger a bounded search of up to three clean alternatives in the same source, each
requiring fresh visual action evidence. Without evidence, the clip is not silently
moved. No additional social search happens during export.

After encoding, the renderer writes `<video>.editorial.json` and chronological review
strips in `<video_stem>_editorial/`. It checks detected cuts against planned cuts,
possible repeated sequences, action/result evidence, and intrusive source text.
The report records actual preflight ranges and scene IDs needing replacement. It
travels with the video when copied to `.renders`.

Statuses:

- `passed`: technical scan and semantic review completed without detected issues.
- `needs_review`: detected issues; `repair_queue` names the affected scenes.
- `unverified`: a required check did not complete; `unverified_scenes` names missing
  semantic reviews. This is never a pass.

Manual export still produces a file. The returned timeline result contains
`editorial_quality`, separate from export success. Unresolved content and source-text
issues are reported for replacement; there is no unbounded regeneration loop.

Checks run by default for scraped clips and timeline exports. Set
`editorial_quality_enabled=false` to explicitly opt out. Semantic review uses the
existing WaveSpeed credentials and V4 vision model, or `editorial_review_model` when
configured. No credentials/API failure means unverified, not approved. The image-based
review cannot prove actions occurring entirely between sampled frames; uncertain
verdicts remain unverified. Cut and repetition detection are heuristics and may need
editor review for deliberate effects.

Tests use synthetic videos and mocked semantic responses, including a real
decoder/encoder render that removes a three-frame flash and honors two explicit
in-points in one source. The existing Walking export can be audited without model
calls by supplying `reviewer=lambda *args: None` to `editorial_quality.audit_export`.
