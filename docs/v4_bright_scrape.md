# Clip Short V4 — Bright editorial sourcing

V4 is isolated in `scrape_v4.py` and does not import a V2/V3 scraper or reuse
project media. Bright Data is the discovery service. A user's already-authorized
TikTok session is a delivery-only recovery path: it may open the *specific post
URL returned by Bright*, but never submits its own keyword search while Bright
has supplied records.

## Sources

1. **Bright TikTok Discovery** batches action-led keywords derived from each
   narrated visual beat. It never appends a platform name to a search term.
2. **Bright Instagram Reels** is a separate collector. Bright's Reels dataset
   supports `url_all_reels`, not reliable keyword discovery, so a small V4
   source-planning call selects raw-footage creator profiles once per project.
   The collector admits a Reel only when its own caption/hashtags ground it in
   a beat; profile membership alone is never treated as relevance.

Both collectors enter one local quality gate: native vertical geometry,
no persistent top-and-bottom letterboxing, visible motion, non-overlapping
source windows, compact three-frame vision review, and a per-source reuse cap.

## Delivery

Bright returns a canonical TikTok post URL as well as a signed media URL with
the discovered record. V4 first tries the configured Bright Web Unlocker zone,
then can consume the same Bright-provided signed asset URL when the target CDN
permits direct delivery. If Bright blocks the CDN but supplied a canonical post
URL, the existing logged-in TikTok context opens that one post and captures the
media response TikTok itself makes for the session. This is not a second TikTok
search and does not spend a keyword-search attempt. An HTML error body is never
accepted as a video merely because it uses an `video/mp4` content type.

As of 2026-09-01, Bright returned `policy_20050` for TikTok CDN host
`v16-webapp-prime.us.tiktok.com`: adding the host to a zone's allowlist does
not override that account-level compliance restriction. TikTok delivery needs
Bright KYC/special permission. The app records that exact rejection instead of
reporting a zero-byte video as a usable candidate.

## Non-negotiable outcome

V4 never fills an uncovered narration beat with a duplicate, a generic clip,
or an empty timeline tile. It writes `review/scrape_v4_report.json` and stops
before rendering if every beat cannot be covered by a unique verified source.

## Scrape.do rendered discovery — measured 2026-09-02

`scrapedo_tiktok.py` renders the TikTok search page and returns canonical
`/@handle/video/id` URLs only. V4 then opens those posts through the user's own
TikTok session; it never runs a local keyword search of its own.

One real query (`日本 自販機`), one request per row:

| setting | unique post URLs | `Scrape.do-Request-Cost` | wall clock |
|---|---|---|---|
| `geoCode=jp`, 3 scrolls | 12 | 5 | 31s |
| `geoCode=sg`, 3 scrolls | 12 | 5 | 26s |
| `geoCode=jp`, 6 scrolls | 12 | 5 | 39s |

Three findings that shape the pipeline:

1. **~12 URLs per query is the ceiling.** Doubling the scroll depth returned the
   SAME twelve posts for eight seconds more. Coverage therefore comes from more
   distinct search hypotheses, never from deeper scrolling; `v4_scrapedo_scrolls`
   above 3 is wasted clock.
2. **Geo swaps the pool, it does not enlarge it.** `jp` and `sg` shared only 2 of
   12 posts - ten were unique to each. So geo is a second axis of breadth, not a
   quality dial; which one is *better* for a Japan script cannot be settled from
   URLs alone and is left to the vision review on a real run.
3. **Cost is charged per rendered request (5), not per URL.** It rides on every
   record as `_scrapedo_cost` and is summed per round into
   `review/scrape_v4_report.json` (`discovery_cost`, `discovery_rounds`), so a
   run can be priced from its own artefact.

The key lives only in the `SCRAPEDO_API_KEY` user environment variable (read via
the registry on Windows when the process env lacks it). It is never written to
the repo, a project folder, a log or the report - covered by
`tests/test_scrapedo_tiktok.py`, which also scans the tree for it.
