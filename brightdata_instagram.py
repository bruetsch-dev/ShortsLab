"""Bright Data Instagram Reels discovery for the scrape pipeline.

There is no keyword search here, and that is a property of the account rather than a gap in this
module: asked for its discovery types, the Reels dataset answers ``url, url_all_reels``, Posts
answers ``url``, and the two datasets that DO search - "Instagram posts search by keyword" and
"Instagram hashtag information" - both answer "This dataset does not support collection". A
hashtag page URL and an /explore/search/ URL were both tried against the reels collector: each
returned the same single unrelated reel from the generic explore page. So Instagram discovery is
account-based, and a caller who wants a topic must name accounts that post that topic.

The API answers in two different shapes for the same request - sometimes a snapshot id to poll,
sometimes the finished records inline as NDJSON - so both are handled here.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

import brightdata_tiktok as brightdata          # shares the API key and endpoint root

# "Instagram - Reels". Overridable for the same reason the TikTok one is: a dataset id is
# account configuration, not a constant of the world.
DATASET_ID = "gd_lyclm20il4r5helnj"
DISCOVER_BY = "url_all_reels"

# A reel run is charged per record like the TikTok one. The ceiling is per process and exists so
# a retry loop cannot turn one Short into an open-ended bill.
RECORD_BUDGET = 60
_RECORDS_USED = 0


def available():
    return brightdata.available()


def records_used():
    return _RECORDS_USED


def reset_budget():
    """Only for tests and for a deliberate new run."""
    global _RECORDS_USED
    _RECORDS_USED = 0


def profile_url(account):
    """Accept '@handle', 'handle', or a full URL and return the profile URL the API expects."""
    text = str(account or "").strip()
    if not text:
        return ""
    if text.startswith(("http://", "https://")):
        return text.split("?", 1)[0].rstrip("/")
    return "https://www.instagram.com/" + text.lstrip("@").strip("/")


def _rows(raw):
    """Parse a response that is either one JSON value or NDJSON.

    The same endpoint returns both, depending on whether the answer was already cached, and a
    plain json.loads() raises "Extra data" on the NDJSON form - which reads like a broken API
    rather than a second valid encoding.
    """
    text = str(raw or "").strip()
    if not text:
        return []
    try:
        value = json.loads(text)
    except ValueError:
        out = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
            except ValueError:
                continue
            if isinstance(parsed, dict):
                out.append(parsed)
        return out
    if isinstance(value, list):
        return [row for row in value if isinstance(row, dict)]
    if isinstance(value, dict):
        for key in ("data", "results", "records", "items"):
            inner = value.get(key)
            if isinstance(inner, list):
                return [row for row in inner if isinstance(row, dict)]
        return [value]
    return []


def _call(url, *, data=None, timeout=120):
    key = brightdata._api_key()
    if not key:
        raise RuntimeError("BRIGHTDATA_API_KEY is not configured")
    headers = {"Authorization": f"Bearer {key}", "Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers,
                                     method="POST" if data is not None else "GET")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return _rows(response.read().decode("utf-8", errors="replace"))


def _normalise(record):
    """One reel in the same shape the rest of the pipeline already reads.

    Every field is defended individually. The TikTok client learned this the hard way: it read an
    author object the payload does not contain and called int() on an ISO timestamp, inside an
    unguarded loop, so a single record took the whole paid batch down and the fallback had never
    returned one usable item.
    """
    url = str(record.get("url") or "")
    shortcode = str(record.get("shortcode") or "")
    post_id = str(record.get("post_id") or record.get("content_id") or shortcode or "")
    if not url and shortcode:
        url = f"https://www.instagram.com/reel/{shortcode}/"
    if not url or not post_id:
        return None
    username = str(record.get("user_posted") or "")
    if not username:
        profile = str(record.get("user_profile_url") or "")
        username = profile.rstrip("/").rsplit("/", 1)[-1] if profile else ""
    # A reel scraped off the generic explore page is attributed to "explore" - it is whatever
    # Instagram happened to show, not a result for anything that was asked. Those are the rows
    # a hashtag or search URL returns, and they are worse than nothing: unrelated footage that
    # still looks like a hit.
    if username.strip().lower() in {"explore", ""}:
        return None
    hashtags = record.get("hashtags")
    hashtags = [str(tag) for tag in hashtags] if isinstance(hashtags, list) else []
    try:
        duration_ms = int(max(0.0, float(record.get("length") or 0.0)) * 1000)
    except (TypeError, ValueError):
        duration_ms = 0

    def count(*keys):
        for key in keys:
            try:
                value = record.get(key)
                if value not in (None, ""):
                    return int(float(value))
            except (TypeError, ValueError):
                continue
        return 0

    return {
        "id": post_id,
        "desc": str(record.get("description") or ""),
        "webVideoUrl": url,
        "_source": "brightdata_instagram",
        "_platform": "instagram",
        # No width/height in this payload either. Left absent on purpose rather than guessed at
        # 9:16 - the ranker scores an unknown shape neutrally, and the true shape is measured
        # from the downloaded file. Inventing one would silence the landscape penalty for good.
        "video": {"width": 0, "height": 0, "duration": duration_ms},
        "stats": {"diggCount": count("likes"),
                  "playCount": count("video_play_count", "views")},
        "createTime": brightdata._epoch(record.get("date_posted") or record.get("timestamp")),
        "author": {"uniqueId": username, "nickname": username},
        "hashtags": hashtags,
        "_followers": count("followers"),
        # The payload hands over a direct CDN link to the file. Downloading the reel PAGE instead
        # means yt-dlp negotiating Instagram without a session - the one thing this mode exists to
        # avoid - so the direct link is carried through and preferred by the downloader.
        "_media_url": str(record.get("video_url") or ""),
        # Top-level "thumbnail" is one of the keys _item_meta reads for the manual browser's
        # cover grid; a private underscore name here left every API reel as a blank tile.
        "thumbnail": str(record.get("thumbnail") or ""),
    }


def reels_for_accounts(accounts, per_account=5, status_cb=None, timeout_s=300):
    """Newest reels for each account. Returns scraper-compatible items.

    ``accounts`` may be handles or profile URLs. This is the only discovery Instagram offers on
    this account, so a topic reaches it as a list of creators who cover that topic.
    """
    global _RECORDS_USED
    urls, seen = [], set()
    for account in accounts or ():
        url = profile_url(account)
        if url and url.lower() not in seen:
            seen.add(url.lower())
            urls.append(url)
    if not available() or not urls:
        return []
    per_account = max(1, min(20, int(per_account or 1)))
    room = RECORD_BUDGET - _RECORDS_USED
    if room <= 0:
        if status_cb:
            status_cb(f"Bright Data Instagram: record budget spent "
                      f"({_RECORDS_USED}/{RECORD_BUDGET}); not requesting more this run.")
        return []
    # Never ask for more than the budget can pay for: the bill is charged per record returned.
    urls = urls[:max(1, room // per_account)]
    endpoint = (f"{brightdata.API_ROOT}/scrape?dataset_id={DATASET_ID}&notify=false"
                f"&include_errors=true&type=discover_new&discover_by={DISCOVER_BY}")
    body = json.dumps(
        {"input": [{"url": url, "num_of_posts": per_account, "country_code": ""} for url in urls],
         "limit_per_input": per_account}, ensure_ascii=False).encode("utf-8")
    if status_cb:
        status_cb(f"Bright Data Instagram: requesting up to {per_account} reel(s) from "
                  f"{len(urls)} account(s).")
    try:
        rows = _call(endpoint, data=body, timeout=min(120, max(30, int(timeout_s))))
    except Exception as exc:                                            # noqa: BLE001
        if status_cb:
            status_cb(f"Bright Data Instagram request failed ({type(exc).__name__}).")
        return []

    # An account that does not exist, or carries no reels, comes back as HTTP 200 with an EMPTY
    # body. Reporting that as "0 usable reel(s)" alongside a real parse failure would send the
    # next reader hunting through the client for a bug that is not there.
    if not rows:
        if status_cb:
            status_cb("Bright Data Instagram: the API returned nothing for "
                      + ", ".join(u.rsplit("/", 1)[-1] for u in urls)
                      + " - no such account, or it has no reels.")
        return []
    snapshot = ""
    if len(rows) == 1 and rows[0].get("snapshot_id") and not rows[0].get("url"):
        snapshot = str(rows[0]["snapshot_id"])
        rows = []
    if not rows and snapshot:
        rows = _await_snapshot(snapshot, timeout_s, status_cb)

    items, broken = [], 0
    for row in rows:
        if row.get("error") or row.get("warning"):
            continue
        try:
            item = _normalise(row)
        except Exception:                                               # noqa: BLE001
            broken += 1
            continue
        if item:
            items.append(item)
    _RECORDS_USED += len(items)
    if status_cb:
        if broken:
            status_cb(f"Bright Data Instagram: skipped {broken} unparsable record(s).")
        status_cb(f"Bright Data Instagram: received {len(items)} usable reel(s). "
                  f"Budget {_RECORDS_USED}/{RECORD_BUDGET}.")
    return items


def _await_snapshot(snapshot, timeout_s, status_cb=None):
    """Poll one snapshot to completion. Measured on the sibling TikTok dataset: submitted at t+0,
    still `running` at t+103, `ready` at t+111 - so a caller that allows only two minutes kills
    the job a few seconds before it pays out."""
    deadline = time.monotonic() + max(60.0, float(timeout_s))
    started, polls = time.monotonic(), 0
    while time.monotonic() < deadline:
        time.sleep(8)
        polls += 1
        try:
            progress = _call(f"{brightdata.API_ROOT}/progress/{snapshot}", timeout=45)
        except Exception:                                               # noqa: BLE001
            continue
        state = str((progress[0] if progress else {}).get("status") or "").casefold()
        if status_cb and (polls == 1 or polls % 4 == 0):
            elapsed = int(time.monotonic() - started)
            status_cb(f"Bright Data Instagram: job still {state or 'processing'} ({elapsed}s elapsed).")
        if state in {"failed", "error", "cancelled", "canceled"}:
            if status_cb:
                status_cb(f"Bright Data Instagram: the job ended as {state!r}.")
            return []
        if state in {"ready", "completed", "complete", "done"}:
            try:
                return _call(f"{brightdata.API_ROOT}/snapshot/{snapshot}?format=json", timeout=120)
            except Exception:                                           # noqa: BLE001
                return []
    if status_cb:
        status_cb("Bright Data Instagram: the job was still running when the clock ran out.")
    return []
