# Social Search and Scrape Research Log

Started: 2026-07-13 (Europe/Zurich)

Status: active research. This file is updated during the work, not reconstructed at the end.

## Objective and constraints

- Find search-term and scrape logic that produces actually relevant real footage for narration.
- Test TikTok, Instagram, and X directly; do not use the app pipeline or WaveSpeed API for research tests.
- Review every accepted result critically and record rejects, failure patterns, adaptations, and decisions.
- Improve both search planning and the upstream visual-script representation.
- When a requested subject remains unavailable, deliberately choose a different physical subject that still communicates the narration beat.

## Baseline inspection (before platform tests)

### Recent script sample and provenance check

The recent project scripts are predominantly fact/culture narrations. The user clarified that all may be AI-written, but tests must exclude scripts produced by the app's own Script Creator. I compared normalized full text against `generated_assets/script_creator_history.json` (12 recorded in-app generations), then also checked fuzzy similarity. Every script below has no exact history match and very low maximum similarity (about 0.03-0.05), so it is treated as externally generated and manually pasted into the app. The project `run_form.json` contains the submitted script but no provenance flag; the history comparison is the strongest available local evidence.

1. **Women / restrictions in Japan** (`no woman in sumo ring`): sumo-ring ban, office heels, glasses at retail jobs, Mount Omine, female royal status. This is the key failure case because the hook object (`sumo ring`) must not become the overall topic.
2. **Gift taboos** (`japan_gift_traps`): four gifts/death homophone, five-cup sets, potted plants in hospitals, retied ribbon symbolism.
3. **Beauty standards** (`japans_brutal_beauty_rules`): sun covers/visors/umbrellas, small-face/CD comparison, facial rollers, makeup for convenience-store trips.
4. **Dating rules** (`japan_dating_rules`): slow fade/ghosting, splitting a bill, work taking priority over a partner.
5. **Nagoro doll village** (`short_1783459687`): life-size dolls at bus stops, fields, roads and abandoned school.

These span literal proof, searchable actions, abstract emotions, culturally specific objects, and a uniquely named location.

### Problems in current code observed before testing

1. `llm_scrape_plan` still asks for a literal query per narration line. This conflicts with the newer bucket planner and encourages over-specific phrases.
2. Retry planning again demands footage that **literally depicts** each failed line. It does not first diagnose *why* the previous query failed or change visual strategy.
3. The structured bucket prompt asks for 5-7 queries in each of four tiers for 8-14 buckets (roughly 160-392 strings). This is excessive, increases near-duplicates, and causes long searches without learning.
4. Queries are described as “TikTok-native” even though the same plan feeds three platforms. X and Instagram have different indexing and useful result types.
5. Current planning can over-weight hook nouns. The women/restrictions script is about institutional control of women, not primarily sumo wrestling.
6. The code contains explicit `TikTok` tokens in some hook/social query strings. Appending a platform name while already searching that platform wastes query precision and can bias toward reposts or discussions about TikTok.
7. Birth-year queries such as `#01 #女の子` are included as a generic cross-platform discovery strategy despite observed zero-result loops on X. A query family needs platform-specific health, not global reuse.
8. The retry agent receives failed narration lines but not the actual query history, result counts, rejection reasons, thumbnails, or platform health. It cannot learn from the run because it is denied the evidence.
9. The acceptance fallback can preserve generic filler when no semantic candidate passes. This makes a completed render look successful while weakening script-to-visual correspondence.

## Evaluation rubric

Each inspected result receives a decision and scores:

- **Narrative relevance (0-4):** 4 = immediately communicates the spoken beat; 2 = valid thematic support; 0 = unrelated.
- **Visual legibility (0-2):** subject/action understandable within about one second on a phone screen.
- **Authenticity (0-2):** genuine creator/local footage rather than stock, slideshow, news composite, or unrelated repost.
- **Editability (0-2):** clean enough, sufficient motion/duration, no dominant burned captions/UI/watermark.

Accept threshold: at least 7/10, with narrative relevance at least 3. A 2/4 thematic result may only be accepted as an explicitly labelled fallback after literal and alternative-subject strategies fail. No result is accepted from title/caption alone; the visible media must be reviewed.

## Initial hypotheses to test

- Short native phrases formed as **subject + action/state** will outperform sentence fragments.
- Named entities and concrete rituals should be searched literally first; abstract claims should search for a physical manifestation, not translated abstractions.
- TikTok likely favors casual phrases, native slang, hashtags, and creator language; Instagram likely favors one strong hashtag/topic plus Reels/account discovery; X likely favors nouns/event phrases and Media/Latest filters, not lifestyle hashtag stacking.
- A query should not be repeated under multiple sorts until the first result page proves the query family has signal.
- Search should adapt after each attempt: zero results -> broaden/rephrase; many irrelevant results -> add discriminating subject/action; relevant but unusable -> keep concept and change platform/form factor.

## Test ledger

### Access/control test A0 — 2026-07-13

| Platform | Direct query | Outcome | Decision / learning |
|---|---|---|---|
| TikTok web | `女人禁制 土俵` | Search page recognized the exact query, then returned TikTok's own “server error” while logged out. | No media evaluated. A zero-result response from this state must be classified as **backend/session failure**, not “bad query”. Retrying 30 query variants cannot fix it. |
| X web, Media | `名頃 かかしの里` | Redirected immediately to login onboarding. | No media evaluated. The scraper must distinguish login wall from 0 results before changing terms. |
| Instagram keyword | `名頃 かかしの里` | Redirected immediately to Instagram login. | No media evaluated. Same session-health requirement. |

The user authorized use of the accounts already used by the app. Those credentials live in separate app browser profiles and may not be extracted or copied. Three direct research tabs were opened in the visible research browser for a one-time user sign-in. Testing will resume there after authentication.

### Public-index sanity test A1 — sumo/women restriction

- `site:tiktok.com 女人禁制 土俵`: TikTok results were blocked to the public index; no candidate accepted.
- `site:x.com 女人禁制 土俵 video`: returned posts using `土俵` metaphorically and ordinary men's sumo coverage. Relevance 0-1/4; all rejected.
- Learning: `土俵` alone is highly polysemous on X. The discriminating concept is not merely “ring”; X needs an event/proof phrase such as `女性 土俵 救命` (the well-known woman/medical-emergency incident), `女性 土俵 降りて`, or an official/news account constraint. For lifestyle b-roll, X is the wrong first platform.

### Public-index sanity test A2 — Nagoro dolls

- English `Nagoro doll village` on X was polluted by generic “doll” and generic “village” posts.
- Native `名頃 かかしの里` produced a false entity collision with `NAGORO BOOKS` in public indexing.
- Learning: named-location queries require an **entity anchor plus visible object**, e.g. `名頃 かかし 徳島`, `天空の村 かかしの里`, or `祖谷 かかし`, rather than romanized `Nagoro` or the ambiguous phrase alone.

No platform result has been accepted yet; title/snippet matches do not satisfy the visual-review rubric.

## Manually authored test query ladders (before authenticated execution)

These terms were authored directly from the externally generated scripts, without the app or an LLM API. They are hypotheses until the authenticated result pages are reviewed.

### Script 1: restrictions imposed on women

**Story spine:** women encountering institutional restrictions across unrelated settings. `sumo` is a hook example only. It must not appear in office, mountain or royal-family searches.

| Narration beat | Physical visual | TikTok ladder | Instagram ladder | X ladder |
|---|---|---|---|---|
| Women barred from a sumo ring | woman at/near a dohyo; incident footage proving exclusion | `女性 土俵` → `女人禁制 相撲` | `#女人禁制` + `#大相撲` | `女性 土俵 救命 filter:media` → `女性 土俵 降りて filter:media` |
| Painful heels at work | woman commuting/working in pumps and reacting to foot pain | `パンプス 痛い` → `ヒール 通勤` → `KuToo パンプス` | `#パンプス通勤` → `#KuToo` | `KuToo パンプス filter:media` |
| Glasses banned on sales floor | retail worker / woman removing glasses; proof/discussion of rule | `メガネ禁止 接客` → `女性 メガネ禁止` | `#販売員メイク` / retail-worker accounts | `女性 メガネ禁止 接客 filter:media` |
| Mount Omine gate | the actual women-exclusion gate/sign and hiker stopping | `大峰山 女人結界` → `女人結界門` | `#大峰山` + place/account inspection | `大峰山 女人禁制 filter:media` |
| Female royal loses status | actual marriage/departure press footage, not generic palaces | `女性皇族 結婚` → `眞子さま 結婚` | `#女性皇族` / `#眞子さま` | `女性皇族 皇籍離脱 filter:media` → `眞子さま 結婚 filter:media` |

Expected hard case: `メガネ禁止 接客`. If exact proof remains caption-heavy or unavailable, the fallback must change the **subject/action** to a clearly uniformed female retail employee adjusting appearance before work. Generic sumo, Tokyo streets, or a random woman are not acceptable.

### Script 2: gift taboos

This script disproves the current blanket rule “never search lifeless objects.” Literal objects and hands demonstrating them are the clearest visual proof here.

| Beat | Primary queries | Intelligent fallback |
|---|---|---|
| Exactly four sweets | `4個 和菓子`, `和菓子 詰め合わせ` | hands counting four sweets into a gift box; reject unrelated candy shelves |
| Sets of five teacups | `湯呑み 5客`, `茶器セット 開封` | hands unboxing/counting a Japanese tea set |
| Potted plant in hospital | `お見舞い 鉢植え`, `病院 お見舞い マナー` | visitor carrying a potted plant toward a hospital; if absent, creator demonstrating prohibited hospital gifts |
| Retieable bow | `蝶結び ラッピング`, `ギフト リボン 結び方` | close-up hands tying/retying a looped bow; this remains highly legible without showing Japan |

### Script 3: beauty standards

- Sun avoidance: `日焼け対策 女子` → `完全防備 紫外線` → `日傘 アームカバー`.
- Small-face comparison: `小顔 CD 比較` → `小顔チャレンジ`.
- Facial rollers: `小顔ローラー` → `小顔マッサージ`.
- Makeup for a tiny errand: `すっぴん コンビニ` → `すっぴん 外出` → `コンビニ メイク`.

### Script 4: dating rules

- Slow fade: `既読無視 あるある` → `返信遅い 恋愛` → `フェードアウト 恋愛`.
- Split bill: `割り勘 デート` → `デート 会計`.
- Work before relationship: `仕事優先 恋愛` → `残業 デート` → `彼氏 会えない`.

### Script 5: Nagoro doll village

- TikTok: `名頃 かかし` → `天空の村 かかし` → `祖谷 かかし`.
- Instagram: `#名頃かかしの里`, `#天空の村かかしの里`, then location/account results.
- X: `名頃 かかし 徳島 filter:media` → `祖谷 かかしの里 filter:media`.

## Emerging design decisions (still subject to platform-test revision)

1. A visual intent needs a **communication role**: `proof`, `demonstration`, `human consequence`, `emotion`, or `pattern interrupt`. “Literal/vibe/shock” alone is too coarse.
2. The same visible concept needs platform-specific executable queries. Reusing one raw array on TikTok, Instagram and X is incorrect.
3. Named entities require disambiguators (location + object), while lifestyle searches need fewer words.
4. Object footage is valid for proof/demonstration beats. The “never lifeless objects” rule should apply only to emotion/vibe fillers, not globally.
5. Search adaptation must consume structured evidence: platform, query, sort/filter, result count, rejection distribution and visible-result descriptions.
6. A session/login/server failure must stop that platform immediately and must not count as a zero-result query.

## Authenticated direct-platform tests

The user completed sign-in in the visible research browser on TikTok, Instagram and X. The tests below use those live platform sessions directly; no app scraper and no WaveSpeed/LLM API generated the queries or evaluated the media.

### TikTok T1 — named entity: Nagoro scarecrow village

Query: `名頃 かかし`

- The first result page contained roughly seven clearly on-topic posts among the first ten inspected results. Exact matches included `@souloftribes.global`, `@alfrenava17`, `@tes_yagi`, `@sei0007` and `@nostalgic_japan`.
- This validates the compact native pattern **entity + visible object** for TikTok.
- Control query `かかし` was heavily polluted by AI fake-news videos, movies, Naruto cosplay, restaurants/cafés named Kakashi, festivals and a different scarecrow village. A named-entity search must therefore never auto-broaden to a single generic token.

Candidate reviews:

| Candidate | Visible review | Score / decision |
|---|---|---|
| `@tes_yagi/video/7275721311312268562` | Authentic row of human-sized dolls and an exact Nagoro caption, but a large burned-in Japanese question covers the center of the frame. | Relevance 4, legibility 2, authenticity 2, editability 0. **Reject**: the caption overlay is a hard editing failure despite topical relevance. |
| `@sei0007/video/7608570015238999303` | Clean vertical snowy Nagoro shot with the local sign and partially snow-covered dolls; no burned captions. The post explicitly describes Nagoro and its roughly 300 dolls. | Relevance 3–4, legibility 1.5, authenticity 2, editability 2. **Accept conditionally**: use a segment where several dolls are visible; the opening sign/snow frame alone does not prove “hundreds.” |

### TikTok T2 — abstract social rule translated into a physical situation

Voiceover concept: leaving home barefaced, even for a convenience-store errand, is treated as sloppy.

Query: `すっぴん コンビニ`

- The page returned multiple exact situation posts, including “メイクしないとコンビニにもいけない”, “すっぴんでコンビニ来ちゃう人”, a late-night convenience-store trip and an awkward encounter while barefaced.
- The native situation phrase is substantially better than generic queries such as “Japan beauty standards”: it names what a phone camera can actually show.

Candidate reviews:

| Candidate | Visible review | Score / decision |
|---|---|---|
| `@__lv118/video/7605048680445529362` | Two casually dressed people outside at night with convenience-store bags; the woman’s face is masked/motion-blurred. No captions, but the visual itself does not communicate the bare-face pressure. | Visual relevance about 2.5. **Reject as primary**; caption-dependent thematic fallback only. |
| `@tamukun_36/video/7145028436690603265` | Clean close-up of a woman’s bare face, no burned captions; exact post caption states she cannot even go to a convenience store without makeup. | Relevance 3, legibility 1.5, authenticity 2, editability 2. **Accept conditionally** as a human-consequence shot if the usable segment shows the makeup/preparation action. |

### Instagram I1 — keyword route versus hashtag route

- Keyword URL `explore/search/keyword/?q=名頃 かかし` returned “No results.”
- Direct hashtag URL `explore/tags/名頃かかしの里/` loaded a dense, highly relevant grid of Nagoro posts, including multiple photos/carousels and Reels.
- This exposes a concrete backend bug: the current Instagram module always builds the keyword-search URL. A leading hashtag must route to `/explore/tags/<tag>/`; otherwise a strong native hashtag is incorrectly reported as zero results.
- Opened Reel `instagram.com/p/DZuI9t6TqPK/`: verified creator, recent post, exact location caption `Nagoro “Scarecrow” Village / 天空の村 かかしの里`, and comments consistent with the real location. It remains **unrated pending frame-level visual inspection**; metadata alone is not enough to accept media.

## Interim empirical changes required

1. Preserve named-entity anchors during query recovery; never reduce `名頃 かかし` to `かかし` merely because a round is weak.
2. Give each intent platform-specific strings and routing. TikTok compact phrases, Instagram hashtags, and X event/proof phrases are not interchangeable.
3. Treat burned-in captions as a hard rejection when they materially cover the subject, even if semantic relevance is perfect.
4. Require visible evidence, not post-caption evidence alone. The `@__lv118` result demonstrates why caption-only matching accepts misleading filler.
5. Instagram hashtag navigation must be implemented before judging hashtag search quality.

### X X1 — named location and proof-event queries

Query `名頃 かかし 徳島` in X's Media tab returned more than thirty media post links, but the loaded set was photo-dominant; no native-video link appeared in the inspected first set. This makes X useful as a secondary proof/image source for this entity, not the first source for moving Nagoro footage.

Query `女性 土俵 救命` was much more effective for the sumo hook than the generic `女性 土俵` family:

- The result set exposed several native-video links immediately.
- Opened candidate `@females_db_park/status/1913409167127417080`: 27-second native video, 1.9M views, 16.1K likes, 2.4K reposts and an exact post description of women administering aid while an official orders them off the ring.
- The poster visibly shows the real arena, ring and crowd with no burned-in caption. The wide opening frame does not yet make the women legible, so this is **accepted conditionally** only for the incident segment where the women enter/provide aid. Metadata + opening frame alone cannot justify using the whole clip.

Control query `名頃 かかし tiktok` returned only five visible photo links in the inspected set, compared with a substantially denser result set for the location-disambiguated native query. Appending the word `tiktok` while already searching X is counterproductive: it narrows toward cross-platform mentions rather than native footage and must be stripped from generated X/TikTok/Instagram terms.

### Instagram I1 candidate frame review completed

The opened Nagoro Reel `instagram.com/p/DZuI9t6TqPK/` reports 80K likes and 476 comments. Its cover clearly shows a fisheye view of a room filled with dolls, but it also carries a large centered Korean/English/Japanese title. Decision: **do not use the intro/cover segment**. The Reel may be accepted only if a later segment is clean and shows the dolls without the title overlay; the scraper must analyze/select a clean subclip rather than rejecting or accepting an entire post from its cover.

This adds a required distinction to media review: caption detection must be **time-local**. A captioned intro does not necessarily invalidate a 40-second source, but the chosen cut interval must be caption-free and visually relevant.

## Implemented V2 corrections (first code pass)

- The Architect now outputs explicit TikTok, Instagram and X query plans per intent and alternative instead of broadcasting the same strings to all backends.
- Each intent carries `communication_role`, `story_subject` and `local_claim`. This prevents a hook example such as sumo from becoming the assumed subject of the whole women/restrictions story.
- Fixed shock logic: no deterministic “every fifth scene” mutation. Pattern interrupts are now allowed only when locally meaningful.
- Restored objects for proof/demonstration roles. The blanket “never lifeless objects” instruction no longer controls factual beats such as four sweets, five cups or a ribbon being tied.
- Platform names (`tiktok`, `instagram`, `x.com`, `reels`, `shorts`, etc.) are stripped at the final execution boundary as well as during plan cleanup.
- X may retain up to four tokens, so a proof phrase such as `女性 土俵 救命` is no longer truncated to `女性 土俵`.
- Query execution now respects each query's platform scope.
- Removed three redundant navigations per query. The login modules fetched the same result neighbourhood and only sorted it locally; V2 now fetches once and combines semantic/engagement ranking afterward.
- Zero-result recovery no longer collapses two-token entity anchors to one token. A 3+ token term may drop only its last disambiguator; two-token failures go to the live adaptive controller.
- The adaptive controller now receives the failed query's platform, communication role, story subject, local claim and visual observations, and returns platform-scoped corrective queries.
- Coverage prioritizes X for proof roles and TikTok/Instagram for action/emotion roles, with at most two platform lanes per scene before secondary terms.
- Instagram now routes `#hashtag` to `/explore/tags/<tag>/`, listens for hashtag responses, parses embedded JSON and retains visible Reel links as a fallback instead of reporting a populated grid as zero results.
- Global lateral filler is TikTok-scoped, preventing generic TikTok-style phrases from causing repeated empty X searches.

Verification:

- `python -m py_compile scrape_v2.py instagram_login.py tests/test_tiktok_scrape_logic.py` passed.
- Three new focused regression tests passed: platform-scoped plan preservation, Instagram hashtag routing, and single-fetch/no-single-token broadening.
- The standalone offline V2 suite `python test_scrape_v2.py` passed in full.

### Additional live controls

TikTok query `パンプス 痛い` (human consequence of painful required footwear):

- Returned a dozen visible results, including exact foot-pain/shoe-rub demonstrations and creators explaining painful pumps.
- Most first-page Japanese results were product/tutorial content rather than a woman visibly suffering during a commute. One Spanish “heels hurt while walking” result was visually promising but loses Japanese authenticity.
- Decision: the query is productive for the **physical consequence**, but it cannot prove the workplace rule. Use it for a close foot-pain/removing-shoes cut, while the rule itself needs a separate proof/news shot or narration support. Do not pretend a shoe product demo proves forced office policy.

X query `女性 メガネ禁止 接客`:

- Returned eight inspected media links, all photos and no native video.
- Decision: X can provide still/news proof for this obscure rule but is a weak footage source for the beat. After this result pattern, the corrective strategy should switch platform and observable action (uniformed sales worker removing glasses/putting in contacts) rather than generating many more X synonyms.

Instagram hashtag controls:

- `#お見舞いマナー` and broader `#お見舞い` both loaded empty tag surfaces in the authenticated direct session.
- This is a valid zero-result strategy outcome, not a login failure: the same session successfully loaded the dense Nagoro tag grid.
- The correct recovery is a changed physical demonstration query on TikTok, not stripping to one generic token or repeating Instagram keyword variants.

Runtime health handling was extended to Instagram: after three consecutive empty searches, one independent `#japan` health probe distinguishes a healthy-but-bad strategy from a broken/login/challenge session. A failed probe disables Instagram for the rest of the run. The corresponding regression test passes.

## Visual-script mapping defect and correction

`apply_visual_script_to_scenes` called `parse_timed_script` even when the visual script contained no timestamps. That parser invents uniform time blocks for ordinary prose, so the later semantic mapping branch was effectively unreachable. Uneven narration scenes could therefore receive the wrong direction, and an early hook noun could bleed into later beats.

Correction:

- The overlap mapper now runs only when the user actually supplied `mm:ss` timestamps.
- Untimed notes are anchored by real scene midpoint/duration, then concrete shared subject/action terms override the temporal guess.
- Generic directions retain chronological order, while specific notes such as “sumo ring”, “painful heels” and “remove glasses” follow their matching local narration claims even when the note list is not perfectly ordered.
- Added regression coverage for both semantic untimed mapping and preserved explicit-timestamp overlap behavior; both pass.

### TikTok T3 — proof/demonstration object beat: retieable gift bow

Query `蝶結び ラッピング` returned twelve highly relevant visible demonstrations in the first inspected result set. This is strong evidence against the old blanket “never search objects” rule: hands tying a bow communicate this narration beat more clearly than generic Japanese people or a reaction face.

Critical candidate reviews:

| Candidate | Visible review | Decision |
|---|---|---|
| `@kurastyle1855/video/7250332628115524865` | 32-second vertical hands-on ribbon demonstration, but the cover/intro has a huge white caption box covering much of the hands and lower frame. | **Reject intro**; later interval may be usable only if segment OCR confirms the box disappears. |
| `@ting_livegood/video/7144159659430186286` | Exact bow demonstration, but the cover has large Chinese headline text and thick horizontal black bars. | **Reject** for caption obstruction and framing/letterbox quality. |

Learning: the query is semantically excellent, yet tutorial neighbourhoods are caption-heavy. Search success and usable-segment success must stay separate. The controller should retain the physical concept but try changed UGC phrasing (`gift opening`, hands untying/retieing) after repeated caption rejections, rather than declaring the concept itself bad.

The live controller now receives cumulative rejection counts (`burned_captions`, `black_bars`, `rapid_edits`, `low_quality`, download failures) in addition to visual descriptions. Its prompt explicitly changes away from tutorial/news-repost neighbourhoods when caption or framing rejection dominates, and changes the visible subject/action when semantic mismatch dominates.

Popularity ranking was also completed: V2 source candidates now retain both likes and views. Relevance remains dominant, but the engagement component blends log-normalized likes and views, so a million-view exact match ranks above an otherwise identical low-view result without allowing a viral off-topic clip to beat a relevant one. Regression coverage passes.

## Field case 2026-08-12: `tokyo_love_goes_private` — 1 of 14 beats got footage

A fact short on Japanese couples produced thirteen still images and one clip. The user's
report was that TikTok has "heaps" of the material. It does. The run never asked for it.

### What the run actually did

| measure | value |
|---|---|
| beats | 14 |
| beats with their own footage | 1 |
| beats that fell back to a still | 13 |
| queries planned | 218 |
| **queries executed** | **4** |
| wall clock | 1977.7s against an 1800s deadline |
| per query | 494s |
| metadata candidates → downloads → segments → quality → semantic | 73 → 36 → 62 → 23 → 0 |

The four queries that ran: `カップル デート vlog` (scene 0), `カップル デート vlog` again
(scene 1), then two adaptive retries `渋谷交差点` and `冷めたカップル` back into the same two
scenes. Scenes 2-13 were never searched.

### The plan was good; the executor never reached it

`search_plan_audit.json` holds 218 queries, 14-16 per scene, and they are the right shape -
the compact native `entity + visible object` pattern this log validated in July:

    scene 6   プリクラ 隠れ家 · ゲーセン デート · #プリクラ
    scene 7   プリクラ 落書き · カップル プリクラ
    scene 10  ラブホ 自動精算機
    scene 11  ラブホ 壁面 · #ラブホテル
    scene 12  ホテルの部屋 扉 · 防音扉 ホテル (X)

None was issued. Five separate mechanisms stacked up:

1. **The generic seeds held the first four slots of every scene.** `カップル デート vlog`,
   `カップル 日常`, `恋人 デート`, `放課後 デート` are identical for 13 of the 14 beats -
   55 of the 218 planned slots are duplicates. In 13 beats the first scene-specific term sat
   at **position 5**. A comment in `queries_for_intent` said the seeds go first so that
   "vague Architect terms must not spend the first browser round"; the seeds turned out to
   be the vague ones.
2. **39 native Japanese queries were discarded before any search**, with the reason
   "Japanese-context scene requires a native Japanese query" - `渋谷デート`,
   `スクランブル交差点`, `東京夜景`, `新宿デート`. The language label came from the JSON key
   the Architect filed the string under (`english` vs `japanese`), not from the text. The
   Architect writes Japanese into the `english` array.
3. **The download budget is global and FIFO.** `max_downloaded_analysis_videos = 36` was
   handed to every call; the first chunk consumed all 36, and from then on
   `_download_and_segment` returned `[]` for every other beat regardless of remaining time.
4. **The coverage queue ran scene-block by scene-block, four at a time**, and each scene
   contributes two entries - so chunk 0 *was* scenes 0 and 1. The adaptive retry can only
   target scenes inside the current chunk, so both corrective queries went back into the
   same two beats.
5. **Nothing ran concurrently.** 35 proxy downloads at a median 34.6s apart = 1211 of the
   1978 seconds, 61% of the run, on network wait.

### The semantic gate was not the problem

`segments_semantic_passed = 0` looks like a broken gate and is not: it is a stale counter,
and the material genuinely did not match. The cached contact sheet for the `渋谷交差点`
round is eight segments of Shibuya city b-roll - crossings, neon, a person dancing on the
crossing - against a beat asking for "young couple walking side by side without touching".
Rejecting them was right. The gate was starved, not broken.

The lesson for this log: **a zero-pass rate is a query symptom, not a threshold symptom.**
Loosening the floors here would have bought worse footage, not more of it.

### Changes made

- language is decided by the text, not by the array it arrived in
- the beat's own terms lead; the deterministic seed follows immediately behind it (an
  Architect term can be junk, so the seed must stay inside the coverage slots - there is a
  regression test for exactly that)
- the coverage queue is round-robin: every beat gets its first search before any beat gets
  a second, so a run that is cut short is cut short evenly
- the download pool is per-round and per-beat, and scales with the beat count; a beat with
  nothing at all raises the cap rather than surrendering to a still
- proxy downloads run six at a time
- **no stills**: a beat that finds nothing borrows motion from the nearest beat that has
  some, at a different in-point, and is labelled `BORROWED` in the report while still
  counting as unmatched
