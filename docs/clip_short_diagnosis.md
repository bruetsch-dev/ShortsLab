# Clip Short — why beats end up empty or duplicated

Measured on `projects/japan_dating_can_start_with_one_terrifying` (11 beats, 2026-08-18).
Every number below comes from that run's own artefacts, not from reading code.

## What the run produced

```
 2 beats  real clip
 8 beats  borrowed from a neighbour
 6 of those 8 show a STILL IMAGE, not video
```

Duplicates are real but hidden behind different filenames — the source id gives them away:

| beat | file | source |
|---|---|---|
| 0 | capblur_01_9a25f13e52.mp4 | 7558781741444386056 |
| 5 | capblur_06_9a25f13e52.mp4 | 7558781741444386056 |
| 1 | capblur_02_9ff8e897dd.mp4 | 7664181386768583956 |
| 10 | capblur_11_9ff8e897dd.mp4 | 7664181386768583956 |

## The funnel

```
13 queries -> 184 raw results -> 58 sources downloaded -> 104 segments
    -> 64 passed quality  ->  2 passed the matcher
```

The search is not the problem. 64 usable clips were bought and analysed; two were used.

## The pool was adequate

Rebuilding the pool offline from the 64 proxies (`tools/manual_match.py`, no network, no API)
gives **134 segments, 92 with a cached description, across 26 distinct sources** — for 11 beats.
Reviewing those 92 descriptions by hand finds a fitting clip for at least 8 of the 11 beats:

| beat | line | fitting footage in the pool |
|---|---|---|
| 2 | "quiet café…" | man at a café table (3 separate sources) |
| 3 | "…kudasai, a clear request to date" | woman at a café table holding up handwritten cards |
| 5 | "the phone lights up" | man looking down at his smartphone |
| 6 | "with LINE messages" | cartoon-bear webpage (LINE's own mascot) |
| 7 | "share stamps, check when they get home" | woman speaking while holding her phone |
| 8 | "step onto a crowded train" | woman walking a train platform beside a train |
| 9/10 | couples in public | three separate matching-outfit couple clips |

## Two distinct defects

**1. The matcher rejects 62 of 64 usable clips.** That is what empties the beats; borrowing and
duplication are only the consequence. Tracked separately.

**2. A clip is thrown away when it is a fraction of a second too short.** The only train shot in
the entire pool is 2.97s; beat 8 needs 3.26s. An 8% shortfall. It was dropped and the beat became
a still. 17 of the 92 segments are shorter than the longest beat, so this is not a one-off.

### Fix for (2), implemented

`SHORT_CLIP_STRETCH_FLOOR = 0.88` in `scrape_v2.py`. A segment that covers at least 88% of the
line is assigned and played slower instead of being discarded; the scene gets `timeline_speed`
and `timeline_speed_src`, which the renderer already turns into a proper CFR30 retime with motion
interpolation. Past 12% short the clip is still rejected, because slower than 0.88x reads as slow
motion on b-roll.

```
2.97s for 3.26s (the real case) -> assigned at 0.91x
2.87s for 3.26s (the limit)     -> assigned at 0.88x
2.80s for 3.26s                 -> still dropped
```

Covered by `test_slightly_short_clip_is_stretched_not_dropped` and
`test_far_too_short_clip_is_still_rejected`.

## Reproducing the offline review

```
python tools/manual_match.py projects/<slug>
```

Rebuilds the segment pool from the proxies already on disk and joins the run's cached vision
descriptions. No network, no API calls. Writes `review/_rebuilt_pool.json`.

---

## Run 2 — `japans_chocolate_confession` (2026-08-18)

User report: *"ich hab nicht eine japanische Frau gesehen ausser in der Hook, und da hats
gelagge, zu lang gezogene Clips dann kommt Standbild, Duplikate."*

### What the run actually did

| stage | number |
|---|---|
| queries planned by the Architect | 126 |
| queries the plan allowed (3/chapter) | 33 |
| **queries actually executed** | **10** |
| of those, hook dance preset | **6** |
| body beats that were searched | **4 of 10** (scenes 1–4) |
| raw results → downloads → segments → semantic pass | 222 → 34 → 74 → 22 |
| beats matched / unmatched | 5 / 5 |
| duplicate guard | 1 swap, **3 unresolved repeats shipped** |

Beats 5–10 — 本命チョコ, ホワイトデー, お返し — were **never searched**. `ホワイトデー お返し`
was planned for scenes 7, 8 and 10 and never ran. The footage could not exist in the pool, so
the last third of the video was filled by borrowing from the first third.

Three of the five body clips came from one single query, `手作りチョコ ラッピング`
(handmade-chocolate wrapping) — hands-only craft footage. That is why no woman appeared
outside the hook.

### The freezes, measured

| beat | needs | clip is | result |
|---|---|---|---|
| 8 | 3.68s | 1.53s (the hook clip) | 2.15s freeze frame |
| 9 | 2.80s | none | PNG still for the whole beat |
| 5 | 3.78s | 2.53s | 1.24s freeze |
| 2 | 3.07s | 2.60s | 0.47s freeze |

### Root causes and fixes

1. **The coverage wave interleaved search with analysis.** It searched four beats, then
   downloaded/described/matched them, then searched the next four — and the first batch's
   analysis consumed the entire 1800s clock (the run overran to 2114s).
   *Fix:* a **breadth sweep** runs one search for every beat before any download or vision
   call, and each analysis round gets a fair slice of the remaining time
   (`_fair_round_deadline`).
2. **The hook ran first and took six of the ten searches.** *Fix:* capped to 3 preset queries,
   given its own 20% sub-deadline, and it now runs after the breadth sweep.
3. **The 2-token TikTok cap split fixed English names.** `Japan White Day` → `Japan White`,
   `Valentine White Day` → `Valentine White`. *Fix:* `_trim_english_query_tokens` spends the
   geo word first and never cuts between two capitalised words.
4. **The borrow path ignored clip length and never retimed.** *Fix:* donor ranking prefers a
   clip that can actually fill the beat; a small shortfall is retimed
   (`_retime_short_scene`, same 12% floor as assignment) instead of frozen; the in-point shift
   no longer eats footage the beat needs.

Regression tests: `test_first_analysis_round_cannot_spend_the_whole_clock`,
`test_a_round_is_never_handed_a_slice_too_short_to_fetch`,
`test_token_cap_does_not_split_a_fixed_english_name`,
`test_geo_word_is_spent_before_a_content_word`,
`test_borrow_prefers_a_donor_long_enough_for_the_beat`,
`test_a_slightly_short_borrow_is_retimed_not_frozen`.

### Still open

- `scrape_clip_id` does not correspond to the clip after a borrow (scene 8 carried scene 5's id
  while playing scene 0's file), so the duplicate guard cannot see some repeats.
- The guard ships `unresolved_repeats` silently into the render; it logs, but nothing blocks.

---

## Run 3 — `you_can_check_into_a_tokyo_hotel` (2026-08-19)

The search and download stages are fixed; the failure moved to the matcher.

| stage | run 2 (chocolate) | run 3 (hotel) |
|---|---|---|
| queries executed | 10 | 13 (10 body + 3 hook, hook now runs LAST) |
| downloads | 34 | **54** |
| segments quality-passed | 74 | **88** |
| **segments the matcher kept** | 22 | **3** |
| beats matched / borrowed | 5 / 5 | 2 / 9 |

The pool was good. It contained ten shots of dinosaurs inside a dinosaur-themed hotel, several
self-checkout clips and a multi-level parking tower. Nine beats still borrowed a neighbour's clip.

### Root cause

The beats were written as actions: *"the dinosaur MOVES behind the desk"*, *"the traveler SPEAKS
toward the raptor"*. Vision correctly answered `literal_match: false` for a static dinosaur
display — and `_evidence_fails` turned that answer into a veto on the **near-miss rescue**, the
one mechanism whose entire job is to stop a beat being empty. The rescue was switched off by
exactly the condition that empties a beat.

Second cause: the context-recovery fallback was gated on a hand-written table of four topics
(chocolate/gift, train/station, christmas, couple). This script is about hotels, checkouts and
parking, so `hits_needed` was empty and the fallback never ran for a single beat.

### Fixes

1. A **wrong subject** is a contradiction and still stays out. The **right subject in the right
   place doing a neighbouring action** is the definition of a near miss and now survives
   (`subj >= 5.5 and loc >= 4.5` with visible evidence).
2. `_intent_context_group` derives the recovery group from the beat's own concrete words, so the
   four-topic table is a boost rather than the gate. A beat with no concrete words
   ("person / stands / indoor area") still recovers nothing.
3. Hook preset queries are deduped before the cap — two birth-year presets sanitise to the same
   `#女の子` and the run spent two of its three hook searches on the identical term.

Fixing (1) also turned the previously-failing `test_strict_relevancy_never_empties_a_scene`
green; that test described this same defect from the coming-of-age run.

New tests: `test_right_subject_wrong_action_is_a_near_miss_not_a_contradiction`,
`test_a_wrong_subject_is_still_refused`,
`test_context_recovery_is_not_limited_to_four_hand_coded_topics`.

### Still open

- The corrective round never ran (13 queries, no adaptive terms) — the clock ended at 2001s.
- `恐竜受付` returned 0 results. The real footage is tagged `変なホテル`; the query planner does
  not know brand names for the places a beat describes.

### Run 3 — the two open items, now fixed

**The corrective round never got any clock.** The analysis rounds were handed the run deadline,
so the last batch could use it up and the one stage that can rescue a beat whose single coverage
query returned nothing never executed. Two runs in a row finished with zero adaptive queries.
`_reserve_for_correction` now holds back 28% (at least 240s) for it, and skips the reserve when
too little time is left for it to buy anything. If no beat needs correcting the time is not
wasted — the secondary query queue still runs to the full deadline.

**The planner described places instead of naming them.** `恐竜受付` returned zero results on
every platform; the footage is tagged `変なホテル`. The Architect prompt now carries a hard rule
with the measured example: when a beat is about a real chain, venue, brand, product, machine or
event, the FIRST query is the name locals type and tag — `変なホテル` over `恐竜受付`,
`くら寿司`/`ビっくらポン` over `皿投入口`, `ユニクロ セルフレジ` over `RFIDレジ`, `エコサイクル`
over `地下駐輪場` — and a descriptive compound is at best the second query.

Test: `test_the_corrective_round_always_keeps_a_share_of_the_clock`.

---

## Goal runs (2026-08-19/20) — where it ended

Two runs of the same script (fake food displays / bathhouse shoe lockers / cat serving robots),
the second after two regressions found in the first.

| | run 1 | run 2 |
|---|---|---|
| queries executed | 25 of 45 | **44** |
| raw results | 319 | **589** |
| downloads | 20 | 29 |
| distinct sources on the timeline | 5 of 15 | **11 of 12** |
| unresolved duplicates | 10 | **0** |
| empty scenes | 0 | **0** |
| beats matched | 4 of 14 | 5 of 12 (3 exact, 2 spare) |

**The two regressions were mine.** The setting anchor was inserted at the FRONT of a beat's query
list and truncated it, so `配膳ロボット` never ran and `飲食店 配膳ロボット` returned four results
instead of a full page - all four robot beats starved. It appends now, and run 2 shows
`配膳ロボット` alone returning 20. And the corrective round walked coverage batches in order, so
eight queries went to beats already scoring 7.2-7.75 while the beats at 2.85 got none; it is
ordered by how many beats a group still has empty.

**What is fixed for good:** empty scenes, duplicate sources and repeated fill material. Run 2 put
eleven distinct sources on twelve beats with zero duplicates - the borrow path hands each empty
beat its own unused candidate rather than replaying a neighbour.

**What is not:** seven of twelve beats are still labelled `borrowed_clip`, i.e. they carry
relevant-looking footage that never cleared the match floor. That is a relevance ceiling, not a
plumbing failure, and the per-beat diagnosis is the tool for it - which is why the matcher's
stats sink is now wired into all nine orchestrator call sites instead of two (run 2 reported
`judged: None` because the paths it actually used were not instrumented).

---

## Zauo re-run (2026-08-20) — the run that searched hard and downloaded nothing

Job `1787250932998`, the same project folder as the earlier good Zauo run, 7200s budget.

| | measured |
|---|---|
| queries executed | 163 |
| raw results | 2476 (tiktok 1850 / instagram 421 / twitter 205) |
| ranked candidates | 897 |
| **downloads** | **11** |
| segments discovered | 15 |
| segments that passed the matcher | 6 of 72 judged |
| beats matched exactly | 2 of 13 |
| beats borrowed / uncovered | 8 / 8 |
| best score on any beat | 5.0 script / 5.4 overall |
| time used | 2969s of 7200s |

Search was healthy. Every earlier fix held: 163 queries against the 10 the chocolate run managed,
2476 raw results, no starvation in the plan. The collapse is entirely between "897 ranked
candidates" and "11 downloads", and the run then stopped with more than an hour unspent because
there was nothing left it believed it was allowed to do.

**Cause.** `_downloaded_ids` answered two different questions with one set. Resume seeds it with
every proxy the project has on disk - "do we already have this source?" - and the download budget
read the same set as "how much has this run spent?". The project held 184 proxies matching the resume glob from earlier
Zauo runs, against a cap of 45, so `_download_and_segment` broke out of its planning loop on the
FIRST source of every round. Every project was good for exactly one well-sourced run; each run
after that starved, and starved silently, because the logs show searching and ranking throughout
and those were never the part that stopped.

The eleven downloads that did happen came from the escalation path, which raises the cap by
`max(2, per_scene//2) * uncovered` per round - it took many rounds to climb past 184.

**Fix.** `_downloaded_ids` stays the dedupe set; `_fetched_ids` counts what this run pulled over
the network and is what the budget and the escalation raise compare against.

Tests: `test_resumed_proxies_do_not_spend_this_run_s_download_budget`,
`test_a_source_already_on_disk_is_never_downloaded_again`.

**Reading this table in future runs:** `downloads` far below `ranked candidates` with time left on
the clock is the signature. Check it before anything else - a matcher that reports low scores on
15 segments is describing the pool it was given, not its own judgement.

---

## Caption blur (2026-08-20) — blurring the picture and leaving the words

Reported on the finished Zauo video: some source captions were still "komplett easy lesen".
Three faults, stacked, each invisible to the check above it.

1. **Colour was never blurred.** `maskedmerge` works per plane, so a gray mask against yuv420p
   video negotiates the mask's chroma planes to a neutral 128: luma was replaced inside the mask
   while colour was blended at 50% across the whole frame. Pink-on-tan captions are almost pure
   chroma, so the letters came through crisp on top of a blurred background - and every
   brightness-based check passed. Measured saturation structure inside the mask: 13.1 against
   30.1 in the original, versus 3.2 with `alphamerge` + `overlay`.
2. **The ink detector knew white and yellow.** Pink matched neither branch, so the mask was
   confetti - about a tenth of the glyph pixels. Ink is now found morphologically in both
   polarities on luma AND saturation.
3. **Glyph masks never cover everything.** At a measured 73-82% stroke coverage the missed
   characters stayed sharp. An OCR-confirmed caption line is now covered outright, rounded at the
   corners. Over 22% of the frame the blur refuses the clip instead: OCR also clusters in-scene
   signage into caption-shaped lines, and on two storefront clips the caption, the shop's own
   board and the faces beneath it would all have gone under one smudge.

Re-checked across ten clips from the pool, five caption styles: captions gone, 2-13% of frame
touched, the two storefront cases bail out.
