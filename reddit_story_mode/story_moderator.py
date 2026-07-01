"""Content safety for generated Reddit-style stories.

Lightweight keyword screen, NOT a full classifier. It flags stories that trip the hard
rules (slurs / gore / self-harm / threats / terrorism / sexual content / real-person
accusations / "this is 100% real" claims) so the generator can drop or regenerate them.
The generation prompt already forbids these; this is a defensive second pass.
"""

import re

# Category -> patterns. Kept deliberately conservative; false positives just drop one of five
# candidates. Word-boundary matching avoids catching innocuous substrings.
_RULES = {
    "self_harm": [r"\bkill (myself|yourself)\b", r"\bsuicid", r"\bself[- ]harm", r"\bhow to (die|hang)\b",
                  r"\bcut(ting)? myself\b"],
    "terrorism": [r"\bbomb\b", r"\bterroris", r"\bmass shoot", r"\bschool shoot", r"\bshoot up (the|my) school\b"],
    "school_threat": [r"\bshoot up\b", r"\bschool threat", r"\bbring a gun to school\b"],
    "graphic_gore": [r"\bdismember", r"\bdisembowel", r"\bmutilat", r"\bgutted\b", r"\bbeheaded\b"],
    "sexual_explicit": [r"\bexplicit sex", r"\bporn\b", r"\brape\b", r"\bmolest"],
    "minor_sexual": [r"\b(child|minor|underage)\b[^.]{0,30}\b(sex|nude|naked)\b"],
    "real_person_accusation": [r"\b(donald trump|joe biden|elon musk|taylor swift)\b"],
    "authenticity_claim": [r"\b100%\s*(real|true)\b", r"\bthis (really|actually) happened, i swear\b",
                           r"\bnot fake\b"],
}

# obvious slurs are matched by a compact set; extend as needed. (kept minimal on purpose)
_SLUR_HINT = re.compile(r"\b(n[i1]gg|f[a4]gg|r[e3]tard)\w*", re.IGNORECASE)


def screen_text(text):
    """Return a list of risk-flag strings for the given text (empty = clean)."""
    blob = str(text or "").lower()
    flags = []
    for category, patterns in _RULES.items():
        for pat in patterns:
            if re.search(pat, blob):
                flags.append(category)
                break
    if _SLUR_HINT.search(blob):
        flags.append("slur")
    return sorted(set(flags))


def moderate_story(story):
    """Attach risk_flags to a story dict and return (is_safe, story). A story is unsafe when
    ANY hard-rule flag is present."""
    text = " ".join(str(story.get(k, "")) for k in ("title", "hook", "summary", "script"))
    flags = screen_text(text)
    story["risk_flags"] = flags
    return (len(flags) == 0), story
