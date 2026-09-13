"""Bright Data TikTok keyword discovery fallback for Scrape V3.

Used only when TikTok's own logged-in search reports its daily search limit.  The API is
asynchronous: submit -> poll snapshot -> download records.  Credentials are read from the
environment or the current user's Windows environment registry and are never written to project
files or logs.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

DATASET_ID = os.environ.get("BRIGHTDATA_TIKTOK_DATASET_ID", "gd_lu702nij2f790tmv9h")
API_ROOT = "https://api.brightdata.com/datasets/v3"

# The per-process record ceiling, so a retry loop cannot turn one Short into an open-ended bill.
# clip_scraper mirrors its own counter for the legacy one-query path; batched search charges here.
BRIGHTDATA_RECORD_BUDGET = 60
_RECORDS_USED = 0


def records_used():
    return _RECORDS_USED


def reset_budget():
    """Only for tests and for a deliberately new run."""
    global _RECORDS_USED
    _RECORDS_USED = 0


def _api_key():
    key = os.environ.get("BRIGHTDATA_API_KEY", "").strip()
    if key or os.name != "nt":
        return key
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as handle:
            value, _ = winreg.QueryValueEx(handle, "BRIGHTDATA_API_KEY")
        return str(value or "").strip()
    except Exception:
        return ""


def available():
    return bool(_api_key() and DATASET_ID)


def _request(url, *, data=None, timeout=90):
    key = _api_key()
    if not key:
        raise RuntimeError("BRIGHTDATA_API_KEY is not configured")
    headers = {"Authorization": f"Bearer {key}", "Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers,
                                 method="POST" if data is not None else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as response:
        body = response.read().decode("utf-8", errors="replace")
        return int(response.status), _parse_body(body)


def _parse_body(body):
    """One JSON value - or NDJSON, one object per line.

    The scrape endpoint answers with BOTH, depending on whether the result was already cached:
    a fresh submit returns a snapshot envelope, a cached one returns the finished records as
    newline-delimited JSON. A plain json.loads() raises "Extra data" on the second form, which
    surfaced as `search_many` "failing" on exactly the queries that had run before - the cache
    made the API look broken.
    """
    text = str(body or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        rows = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
            except ValueError:
                continue
            if isinstance(parsed, dict):
                rows.append(parsed)
        return rows if rows else None


def _records(payload):
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        for key in ("data", "results", "records", "items"):
            value = payload.get(key)
            if isinstance(value, list):
                return [row for row in value if isinstance(row, dict)]
    return []


def _epoch(value):
    """Bright Data stamps times as ISO-8601 ("2026-06-06T09:05:47.000Z"), not as Unix seconds.
    int() on that raised ValueError, and because the caller normalised records in an unguarded
    loop, ONE record took the whole batch down - which is why the API fallback had never
    returned a single usable item."""
    if value in (None, ""):
        return 0
    try:
        return int(float(value))
    except (TypeError, ValueError):
        pass
    text = str(value).strip().replace("Z", "+00:00")
    try:
        from datetime import datetime
        return int(datetime.fromisoformat(text).timestamp())
    except (ValueError, OSError):
        return 0


def _normalise(record):
    author = record.get("author") if isinstance(record.get("author"), dict) else {}
    # Bright can put its signed CDN asset in ``url``.  That link is useful as
    # a last resort, but it is *not* the URL we want to open in the user's
    # logged-in TikTok browser: opening it cannot establish a post session.
    # Prefer a canonical post URL whenever the payload exposes a creator and
    # post id, then keep the CDN address separately in ``_media_url``.
    supplied_url = str(record.get("url") or record.get("post_url")
                       or record.get("webpage_url") or "")
    url = supplied_url
    post_id = str(record.get("post_id") or record.get("aweme_id") or record.get("id") or "")
    desc = str(record.get("description") or record.get("desc") or record.get("text")
               or record.get("caption") or "")
    # The real payload carries no "author" object at all: the @handle is account_id and the
    # display name is profile_username. Reading the old keys left every item with an empty
    # author, which the downstream matcher uses for attribution and de-duplication.
    username = str(record.get("account_id") or record.get("author_name")
                   or record.get("author_username") or record.get("username")
                   or author.get("unique_id") or author.get("uniqueId")
                   or author.get("name") or "")
    if not username:
        profile = str(record.get("profile_url") or "")
        if "/@" in profile:
            username = profile.rsplit("/@", 1)[1].split("/")[0]
    nickname = str(record.get("profile_username") or record.get("nickname") or username)
    if post_id and username:
        url = f"https://www.tiktok.com/@{username.lstrip('@')}/video/{post_id}"
    elif not url:
        url = str(record.get("video_url") or record.get("cdn_url") or "")
    if not post_id and url:
        import re
        match = re.search(r"/video/(\d+)", url)
        post_id = match.group(1) if match else url
    if not url or not post_id:
        return None
    likes = record.get("digg_count") or record.get("likes") or record.get("like_count") or 0
    views = record.get("play_count") or record.get("views") or record.get("view_count") or 0
    # The payload carries `width` and a `ratio` quality class ("720p") but NO height field at
    # all. Inventing one would be worse than leaving it out: claiming 9:16 for every record would
    # silence the landscape penalty that keeps wide clips out of a vertical Short. The true shape
    # is measured from the downloaded file anyway; what matters here is that "unknown" reaches
    # the ranker as unknown rather than as zero.
    width = record.get("width") or 0
    height = record.get("height") or 0
    duration = record.get("duration") or record.get("video_duration") or 0
    try:
        duration_ms = int(float(duration) * (1 if float(duration) > 1000 else 1000))
    except (TypeError, ValueError):
        duration_ms = 0
    hashtags = record.get("hashtags") if isinstance(record.get("hashtags"), list) else []
    return {
        "id": post_id,
        "desc": desc,
        "webVideoUrl": url,
        "_source": "brightdata_tiktok",
        "_platform": "tiktok",
        "video": {"width": int(width or 0), "height": int(height or 0),
                  "duration": duration_ms, "ratio": str(record.get("ratio") or "")},
        "stats": {"diggCount": int(likes or 0), "playCount": int(views or 0)},
        "createTime": _epoch(record.get("create_time") or record.get("created_at")
                             or record.get("timestamp")),
        "author": {"uniqueId": username, "nickname": nickname},
        "hashtags": hashtags,
        # Same as the Instagram client: the response already contains a direct link to the file,
        # so the downloader does not have to re-negotiate the page it came from.
        "_media_url": str(record.get("video_url") or record.get("cdn_url")
                          or record.get("cdn_link") or ""),
        # The timeline's manual browser is a grid of covers; an item without one renders as a
        # blank tile. _item_meta reads top-level "cover", and the payload calls it preview_image.
        "cover": str(record.get("preview_image") or ""),
    }


def search_many(queries, per_query=10, status_cb=None, timeout_s=420, retry=True,
                max_records=None):
    """Every keyword in ONE job. Returns {query: [item, ...]}.

    This is the difference between the API being usable and not. A discover job spends 60-110
    seconds queued and running, almost all of it waiting, and the endpoint accepts an input ARRAY
    - so eight keywords cost one wait, not eight. Run serially a chapter spent its whole clock on
    two queries and downloaded nothing; batched, the same eight arrive inside one budget.

    Records are attributed back to their keyword through `discovery_input`, which the API stamps
    on every row.
    """
    global _RECORDS_USED
    wanted = []
    for entry in queries or ():
        text = str(entry or "").strip()
        if text and text not in wanted:
            wanted.append(text)
    if not available() or not wanted:
        return {}
    per_query = max(1, min(10, int(per_query or 1)))
    room = BRIGHTDATA_RECORD_BUDGET - _RECORDS_USED
    if room <= 0:
        if status_cb:
            status_cb(f"Bright Data TikTok: record budget spent ({_RECORDS_USED}/"
                      f"{BRIGHTDATA_RECORD_BUDGET}); no further keywords this run.")
        return {}
    # A caller may hand this call its own ceiling (a chapter's fair share). One chapter once
    # spent the entire run budget - 70 of 60, because charging happened only when records
    # arrived - and every later chapter searched with nothing left.
    if max_records is not None:
        room = min(room, max(0, int(max_records)))
        if room < per_query:
            if status_cb:
                status_cb("Bright Data TikTok: this chapter's record share is spent.")
            return {}
    # Never submit more keywords than the budget can pay for - the bill is per record returned.
    wanted = wanted[:max(1, room // per_query)]
    # Reserve NOW, reconcile after: charging on arrival let a second job start while the first
    # was still counting, and the run overshot its own ceiling by a whole job.
    reserved = len(wanted) * per_query
    _RECORDS_USED += reserved
    endpoint = (f"{API_ROOT}/scrape?dataset_id={DATASET_ID}&notify=false&include_errors=true"
                "&type=discover_new&discover_by=keyword")
    body = json.dumps({"input": [{"search_keyword": q, "country": ""} for q in wanted],
                       "limit_per_input": per_query}, ensure_ascii=False).encode("utf-8")
    if status_cb:
        status_cb(f"Bright Data TikTok: one job for {len(wanted)} keyword(s), "
                  f"up to {per_query} record(s) each.")
    payload = None
    for attempt in (1, 2):
        try:
            _status, payload = _request(endpoint, data=body,
                                        timeout=min(120, max(30, timeout_s)))
        except Exception as exc:                                        # noqa: BLE001
            if status_cb:
                status_cb(f"Bright Data TikTok request failed ({type(exc).__name__}).")
            return {}
        if payload is not None:
            break
        if attempt == 1:
            time.sleep(5)
    if payload is None:
        if status_cb:
            status_cb("Bright Data TikTok: the batched submit came back empty twice "
                      "(account busy?); nothing was charged and nothing returned.")
        return {}
    rows = _records(payload)
    snapshot = str(payload.get("snapshot_id") or "") if isinstance(payload, dict) else ""
    if not rows and snapshot:
        rows = _await(snapshot, timeout_s, status_cb)

    out, broken, failed = {q: [] for q in wanted}, 0, 0
    for row in rows:
        # An input that dies comes back as a ROW, not as an absent keyword: {"warning": ...} or
        # {"error": "Something went wrong"} with error_codes like {"dead_page": 2} on the job.
        # Feeding those to _normalise produced nothing and silently turned "this keyword was
        # never actually searched" into "this keyword found nothing" - two facts that call for
        # opposite responses, and the reason a query returned 0 one minute and 10 the next.
        # Inside the try, not before it: a record malformed enough that .get() itself raises
        # must still cost one record rather than the whole paid batch. That is the exact defect
        # this module was fixed for once already.
        try:
            if row.get("error") or row.get("warning"):
                failed += 1
                continue
            item = _normalise(row)
        except Exception:                                               # noqa: BLE001
            broken += 1
            continue
        if not item:
            continue
        source = row.get("discovery_input") if isinstance(row.get("discovery_input"), dict) else {}
        keyword = str(source.get("search_keyword") or "").strip()
        # An unattributed row is still a paid, usable clip: give it to the first keyword rather
        # than dropping it because its provenance stamp was missing.
        out.setdefault(keyword if keyword in out else wanted[0], []).append(item)
    if failed:
        empty = [q for q in wanted if not out.get(q)]
        if status_cb:
            status_cb(f"Bright Data TikTok: {failed} input(s) died on the provider side (dead "
                      f"page) - {', '.join(empty[:4]) or 'unknown'} were NOT searched, which is "
                      f"not the same as searched-and-empty.")
        # dead_page is transient: the same keyword that died here returned ten records minutes
        # earlier. One retry for the dead inputs only, and only if the budget can still pay for
        # them - it is the difference between a chapter with no footage and a chapter with some.
        room_left = BRIGHTDATA_RECORD_BUDGET - _RECORDS_USED - sum(len(v) for v in out.values())
        if empty and retry and room_left >= per_query:
            if status_cb:
                status_cb(f"Bright Data TikTok: retrying {len(empty)} dead input(s) once.")
            second = search_many(empty, per_query=per_query, status_cb=status_cb,
                                 timeout_s=timeout_s, retry=False)
            for keyword, items in (second or {}).items():
                if items:
                    out[keyword] = items
    found = sum(len(v) for v in out.values())
    # Reconcile the reservation against what actually arrived - dead inputs are not billed.
    _RECORDS_USED += found - reserved
    if status_cb:
        if broken:
            status_cb(f"Bright Data TikTok: skipped {broken} unparsable record(s).")
        hits = ", ".join(f"{len(v)}x {q[:22]}" for q, v in out.items() if v) or "nothing"
        status_cb(f"Bright Data TikTok: {found} record(s) across {len(wanted)} keyword(s) "
                  f"({hits}). Budget {_RECORDS_USED}/{BRIGHTDATA_RECORD_BUDGET}.")
    return out


def _await(snapshot, timeout_s, status_cb=None):
    """Poll one snapshot to completion, naming the ending rather than reporting every one as 0."""
    deadline = time.monotonic() + max(30, float(timeout_s))
    polls, last_state, poll_errors = 0, "", 0
    while time.monotonic() < deadline:
        time.sleep(8)
        polls += 1
        try:
            _status, progress = _request(f"{API_ROOT}/progress/{snapshot}", timeout=45)
        except Exception:                                               # noqa: BLE001
            poll_errors += 1
            continue
        state = str((progress or {}).get("status") or "").casefold()
        last_state = state or last_state
        if status_cb and (polls == 1 or polls % 4 == 0):
            status_cb(f"Bright Data TikTok: job still {state or 'processing'} "
                      f"({int(max(0, deadline - time.monotonic()))}s budget remaining).")
        if state in {"failed", "error", "cancelled", "canceled"}:
            if status_cb:
                status_cb(f"Bright Data TikTok: the job ended as {state!r} after {polls} poll(s).")
            return []
        if state in {"ready", "completed", "complete", "done"}:
            try:
                _status, payload = _request(f"{API_ROOT}/snapshot/{snapshot}?format=json",
                                            timeout=120)
                return _records(payload)
            except Exception as exc:                                    # noqa: BLE001
                if status_cb:
                    status_cb(f"Bright Data TikTok: ready but the snapshot could not be read "
                              f"({type(exc).__name__}).")
                return []
    if status_cb:
        status_cb(f"Bright Data TikTok: gave up after {int(float(timeout_s))}s with the job still "
                  f"{last_state or 'unreported'} ({polls} poll(s), {poll_errors} unanswered).")
    return []


def search(query, limit=10, status_cb=None, timeout_s=600):
    """Discover up to ``limit`` TikToks for one keyword and return scraper-compatible items."""
    if not available() or not str(query or "").strip() or int(limit or 0) <= 0:
        return []
    limit = max(1, min(10, int(limit)))
    endpoint = (f"{API_ROOT}/scrape?dataset_id={DATASET_ID}&notify=false&include_errors=true"
                "&type=discover_new&discover_by=keyword")
    body = json.dumps({"input": [{"search_keyword": str(query).strip(), "country": ""}],
                       "limit_per_input": limit}, ensure_ascii=False).encode("utf-8")
    if status_cb:
        status_cb(f"Bright Data TikTok fallback: requesting up to {limit} record(s) for {query!r}.")
    payload = None
    for attempt in (1, 2):
        try:
            status, payload = _request(endpoint, data=body, timeout=min(120, max(30, timeout_s)))
        except Exception as exc:
            if status_cb:
                status_cb(f"Bright Data TikTok request failed ({exc.__class__.__name__}).")
            return []
        if payload is not None:
            break
        # Observed live: HTTP 200 with an EMPTY body on submit - no snapshot, no rows, no error.
        # It happens when the account is already busy with concurrent jobs, and it is transient:
        # the same query resubmitted seconds later returns a snapshot id. One retry, then a
        # named outcome instead of a silent zero.
        if attempt == 1:
            time.sleep(5)
    if payload is None:
        if status_cb:
            status_cb("Bright Data TikTok: the submit came back empty twice (account busy?); "
                      "this query returned nothing THIS attempt - it is worth retrying later.")
        return []
    rows = _records(payload)
    snapshot_id = str(payload.get("snapshot_id") or "") if isinstance(payload, dict) else ""
    if not rows and snapshot_id:
        # "received 0 usable record(s)" used to be the report for four different endings: the job
        # failed, the job was still running when the clock ran out, the job genuinely found
        # nothing, or every record was unparsable. They need completely different responses -
        # retry, wait longer, change the query, fix the mapping - so they are named separately.
        deadline = time.monotonic() + max(30, float(timeout_s))
        polls, last_state, poll_errors = 0, "", 0
        while time.monotonic() < deadline:
            time.sleep(8)
            polls += 1
            try:
                _, progress = _request(f"{API_ROOT}/progress/{snapshot_id}", timeout=45)
            except Exception:                                           # noqa: BLE001
                poll_errors += 1
                continue
            state = str((progress or {}).get("status") or "").casefold()
            last_state = state or last_state
            if state in {"failed", "error", "cancelled", "canceled"}:
                if status_cb:
                    status_cb(f"Bright Data TikTok: the job ended as {state!r} after "
                              f"{polls} poll(s); nothing was returned.")
                return []
            if state in {"ready", "completed", "complete", "done"}:
                try:
                    _, payload = _request(
                        f"{API_ROOT}/snapshot/{snapshot_id}?format=json", timeout=120)
                    rows = _records(payload)
                except Exception as exc:                                # noqa: BLE001
                    if status_cb:
                        status_cb(f"Bright Data TikTok: the job was ready but its snapshot could "
                                  f"not be read ({type(exc).__name__}).")
                    rows = []
                break
        else:
            if status_cb:
                status_cb(f"Bright Data TikTok: gave up after {int(float(timeout_s))}s with the "
                          f"job still {last_state or 'unreported'} "
                          f"({polls} poll(s), {poll_errors} unanswered). The job keeps running "
                          f"on their side - a longer budget would have collected it.")
            return []
    out, broken = [], 0
    for row in rows:
        # One unexpected field must cost one record, never the whole paid batch - so the
        # provider-error check lives INSIDE the guard too. A failure arrives as a row, not as an
        # absence; parsing it would report "found nothing" for a keyword never actually searched.
        try:
            if row.get("error") or row.get("warning"):
                continue
            item = _normalise(row)
        except Exception:                                               # noqa: BLE001
            broken += 1
            continue
        if item:
            out.append(item)
    if broken and status_cb:
        status_cb(f"Bright Data TikTok: skipped {broken} record(s) this response could not parse.")
    if status_cb:
        status_cb(f"Bright Data TikTok fallback: received {len(out)} usable record(s).")
    return out[:limit]
