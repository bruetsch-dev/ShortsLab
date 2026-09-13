"""Rendered TikTok search discovery through Scrape.do.

This module intentionally stops at canonical TikTok post URLs.  It does not
download media, use a local browser, or fabricate metadata.  V4 subsequently
opens only the selected posts through the user's existing TikTok session and
runs its normal technical and visual gates.
"""
from __future__ import annotations

import html
import json
import os
import re
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed


API_ROOT = "https://api.scrape.do/"
POST_RE = re.compile(r"(?:https?://(?:www\.)?tiktok\.com)?/@([A-Za-z0-9._-]+)/video/(\d{6,})", re.I)


def _api_key():
    key = os.environ.get("SCRAPEDO_API_KEY", "").strip()
    if key or os.name != "nt":
        return key
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as handle:
            value, _ = winreg.QueryValueEx(handle, "SCRAPEDO_API_KEY")
        return str(value or "").strip()
    except Exception:
        return ""


def available():
    return bool(_api_key())


def browser_actions(scroll_steps=3):
    """A bounded search-page interaction sequence, kept testable and explicit."""
    # Three, never more. Scrape.do measured this for us on 2026-09-02: 3 scrolls returned ~14
    # videos in ~28s, 6 scrolls returned the SAME ~14 in ~38s, and 10 timed out and returned
    # nothing. Coverage comes from more search TERMS, not from scrolling one term deeper - which
    # matches what this pipeline measured independently (a hard ~12 URL ceiling per query).
    steps = max(1, min(3, int(scroll_steps or 3)))
    actions = [{"Action": "Wait", "Timeout": 13000}]
    for _ in range(steps):
        actions.extend([
            {"Action": "Execute", "Execute": "window.scrollTo(0, document.body.scrollHeight);"},
            {"Action": "Wait", "Timeout": 3500},
        ])
    return actions


def target_search_url(query):
    # The target itself owns a query string.  urllib.urlencode below encodes
    # this URL a second time for Scrape.do, producing the required `%2520`.
    return "https://www.tiktok.com/search?" + urllib.parse.urlencode({"q": str(query or "")})


def extract_post_urls(markup):
    """Return canonical, unique post URLs from rendered search HTML."""
    text = html.unescape(str(markup or ""))
    text = text.replace("\\u002F", "/").replace("\\/", "/")
    found, seen = [], set()
    for handle, video_id in POST_RE.findall(text):
        key = (handle.casefold(), video_id)
        if key in seen:
            continue
        seen.add(key)
        found.append(f"https://www.tiktok.com/@{handle}/video/{video_id}")
    return found


def search(query, *, geo_code="jp", scroll_steps=3, timeout_s=75, status_cb=None):
    """Render one TikTok search page and return URL-only candidate records."""
    key = _api_key()
    if not key:
        raise RuntimeError("SCRAPEDO_API_KEY is not configured")
    target = target_search_url(query)
    params = {
        "token": key,
        "geoCode": str(geo_code or "jp"),
        "render": "true",
        "playWithBrowser": json.dumps(browser_actions(scroll_steps), separators=(",", ":")),
        "url": target,
    }
    request = urllib.request.Request(API_ROOT + "?" + urllib.parse.urlencode(params),
                                     headers={"User-Agent": "Shortslab/1.0", "Accept": "text/html"})
    try:
        with urllib.request.urlopen(request, timeout=max(30, int(timeout_s or 75))) as response:
            markup = response.read().decode("utf-8", errors="replace")
            cost = str(response.headers.get("Scrape.do-Request-Cost") or "")
    except Exception as exc:
        raise RuntimeError(f"Scrape.do TikTok search failed ({type(exc).__name__})") from exc
    urls = extract_post_urls(markup)
    if status_cb:
        suffix = f" · {cost} credits" if cost else ""
        status_cb(f"Scrape.do TikTok: {len(urls)} unique post URL(s) for {str(query)[:52]!r}{suffix}.")
    # The cost header rides along on every record. Reading it only for the status line meant the
    # run could not report what discovery actually cost, and a status line is not an artefact.
    return [{"id": video_id, "webVideoUrl": url, "url": url, "desc": "",
             "_platform": "tiktok", "_source": "scrapedo_tiktok", "_search_query": str(query),
             "_scrapedo_cost": cost, "_scrapedo_geo": str(geo_code or "jp")}
            for url in urls
            for _handle, video_id in [POST_RE.search(url).groups()]]


def total_cost(grouped_or_records):
    """Sum the per-request Scrape.do cost across a search result.

    Accepts either the {query: [record, ...]} mapping from `search_many` or a flat record list.
    One cost is charged per RENDERED REQUEST, not per URL, so it is counted once per query.
    """
    if isinstance(grouped_or_records, dict):
        buckets = list(grouped_or_records.values())
    else:
        by_query = {}
        for record in grouped_or_records or ():
            by_query.setdefault(str((record or {}).get("_search_query") or ""), []).append(record)
        buckets = list(by_query.values())
    total = 0.0
    for bucket in buckets:
        for record in bucket or ():
            try:
                total += float(str((record or {}).get("_scrapedo_cost") or "").strip() or 0)
            except (TypeError, ValueError):
                pass
            break                       # charged per request, so once per query bucket
    return round(total, 4)


def search_many(queries, *, geo_code="jp", scroll_steps=3, workers=4, timeout_s=75, status_cb=None):
    """Search independent hypotheses concurrently while retaining query attribution."""
    unique = []
    for query in queries or ():
        value = str(query or "").strip()
        if value and value.casefold() not in {item.casefold() for item in unique}:
            unique.append(value)
    if not unique:
        return {}
    worker_count = max(1, min(4, int(workers or 4), len(unique)))
    if status_cb:
        status_cb(f"Scrape.do TikTok: rendering {len(unique)} search page(s) with {worker_count} worker(s).")
    results = {query: [] for query in unique}
    with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="scrapedo-search") as pool:
        futures = {pool.submit(search, query, geo_code=geo_code, scroll_steps=scroll_steps,
                               timeout_s=timeout_s, status_cb=status_cb): query for query in unique}
        for future in as_completed(futures):
            query = futures[future]
            try:
                results[query] = future.result()
            except Exception as exc:
                if status_cb:
                    status_cb(f"Scrape.do TikTok: search {query[:42]!r} failed ({type(exc).__name__}).")
    return results
