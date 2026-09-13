"""Region/topic profiles selected by a <tag> at the top of the script.

Why this exists: the scrape path used to be hard-wired to Japan - the query prompts demanded
"mostly native Japanese", the hook finder always searched Tokyo street-style hashtags, and the
X proof terms contained salaryman/convenience store. That was right for the Japanese-facts
channel but made every other topic (US, Swiss, historical) search in the wrong language.

A profile turns those hard-wired strings into data. The `japan` profile holds the ORIGINAL
strings verbatim, so a script tagged <japan> produces byte-identical prompts/terms to before.

Usage in a script (first line, stripped before TTS/captions):
    <japan>        -> the original Japanese-first behaviour
    <general>      -> English only, no Japan anything (same as no tag at all)
    <switzerland>  -> German + English
    <history>      -> English, archival/historical framing

`multi_language_search` (a checkbox on the script step) widens the search languages instead of
using only the region default.
"""

import re

# ---------------------------------------------------------------- tag parsing

REGION_TAGS = ("japan", "general", "switzerland", "history")
DEFAULT_REGION = "general"          # no tag == <general> (user spec)

_TAG_RE = re.compile(r"<\s*(%s)\s*>" % "|".join(REGION_TAGS), re.IGNORECASE)


def parse_region_tag(script):
    """Return ``(region, script_without_tags)``.

    The tag is REMOVED from the script: it is an authoring directive, never something the
    narrator should speak or the captions should show. First tag wins; unknown <...> text is
    left untouched. No tag -> DEFAULT_REGION.
    """
    text = str(script or "")
    found = _TAG_RE.search(text)
    region = found.group(1).lower() if found else DEFAULT_REGION
    cleaned = _TAG_RE.sub("", text)
    # a tag on its own line leaves a blank first line behind
    cleaned = re.sub(r"^[ \t]*\n", "", cleaned)
    return region, cleaned.strip()


def strip_region_tags(script):
    """Just the cleaned script (convenience for callers that don't need the region)."""
    return parse_region_tag(script)[1]


# ---------------------------------------------------------------- profiles

# The japan entry MUST keep the exact strings the code used before this module existed, so
# <japan> reproduces the old output. Do not "improve" them.
_JAPAN_WOMAN_LEAD = "beautiful japanese woman, tokyo street style, fashionable, candid"
_JAPAN_WOMAN_TAGS = ["japanesegirl", "tokyofashion", "ootdjapan", "japanstyle",
                     "tokyostreetstyle", "fyp"]
_JAPAN_PROOF_EXTRA = {"salaryman", "convenience store", "students", "cleaning", "classroom",
                      "commuter"}

PROFILES = {
    "japan": {
        "label": "Japan",
        # native_lang drives the query prompt wording; ja keeps the original JP-first planner
        "native_lang": "ja",
        "native_lang_name": "Japanese",
        "search_langs": ["ja", "en"],       # ja primary, en secondary (original behaviour)
        "woman_lead": _JAPAN_WOMAN_LEAD,
        "woman_tags": _JAPAN_WOMAN_TAGS,
        "proof_extra": _JAPAN_PROOF_EXTRA,
        "legacy_japan_prompt": True,        # take the verbatim original prompt branch
    },
    "general": {
        "label": "General",
        "native_lang": "en",
        "native_lang_name": "English",
        "search_langs": ["en"],
        "woman_lead": "attractive woman, street style, fashionable, candid",
        "woman_tags": ["streetstyle", "ootd", "fashiontiktok", "grwm", "fyp"],
        "proof_extra": set(),
        "legacy_japan_prompt": False,
    },
    "switzerland": {
        "label": "Switzerland",
        "native_lang": "de",
        "native_lang_name": "German",
        "search_langs": ["de", "en"],       # German by default (user spec)
        "woman_lead": "attractive swiss woman, street style, fashionable, candid",
        "woman_tags": ["schweiz", "switzerland", "zurich", "streetstyle", "fyp"],
        "proof_extra": {"sbb", "zug", "bahnhof", "migros", "coop", "alp", "berg"},
        "legacy_japan_prompt": False,
    },
    "history": {
        "label": "History",
        "native_lang": "en",
        "native_lang_name": "English",
        "search_langs": ["en"],
        # historical topics rarely have influencer-style hooks; keep it neutral
        "woman_lead": "historical reenactment, archive footage, museum",
        "woman_tags": ["history", "historytok", "archive", "reenactment", "fyp"],
        "proof_extra": {"archive", "museum", "reenactment", "artifact", "ruins", "excavation"},
        "legacy_japan_prompt": False,
    },
}

# Languages added on top of the region's own when the user ticks "multi-language search".
MULTI_SEARCH_LANGS = ["en", "ja", "zh", "es"]

_LANG_NAMES = {"ja": "Japanese", "en": "English", "de": "German",
               "zh": "Chinese", "es": "Spanish", "ko": "Korean"}


def lang_name(code):
    return _LANG_NAMES.get(str(code or "").lower(), str(code or "").upper())


def get_profile(region=None, multi_language=False, languages=None):
    """Return a copy of the profile for ``region`` (unknown -> DEFAULT_REGION).

    With ``multi_language`` the search_langs widen to the region's own languages plus
    MULTI_SEARCH_LANGS (deduped, region's native first so it still leads the search).
    """
    key = str(region or DEFAULT_REGION).strip().lower()
    if key not in PROFILES:
        key = DEFAULT_REGION
    profile = dict(PROFILES[key])
    profile["region"] = key
    # Explicit per-language checkboxes (2026-07-22, replaces the single multi-language
    # toggle): the user's picks BECOME the search languages, region-native codes first.
    picked = [str(c).strip().lower() for c in (languages or []) if str(c).strip()]
    if picked:
        native = [c for c in profile["search_langs"] if c in picked]
        rest = [c for c in picked if c not in native]
        profile["search_langs"] = native + rest
        if profile["search_langs"] != list(PROFILES[key]["search_langs"]):
            profile["legacy_japan_prompt"] = False
        profile["multi_language"] = len(profile["search_langs"]) > 1
        return profile
    if multi_language:
        langs = list(profile["search_langs"])
        for code in MULTI_SEARCH_LANGS:
            if code not in langs:
                langs.append(code)
        profile["search_langs"] = langs
        # a widened search is no longer the original JP-only plan
        profile["legacy_japan_prompt"] = False
    profile["multi_language"] = bool(multi_language)
    return profile


def profile_for_script(script, multi_language=False):
    """Convenience: parse the tag out of a script and return (profile, cleaned_script)."""
    region, cleaned = parse_region_tag(script)
    return get_profile(region, multi_language=multi_language), cleaned
