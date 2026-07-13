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
