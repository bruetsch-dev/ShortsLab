"""Human-in-the-loop SFX categorization trainer.

The SFX Master used to guess every sound's role from its FILENAME (so `fast-fart`
became a "bright_whoosh"). This tool replaces that guessing with GROUND TRUTH the
user confirms BY EAR - exactly like the scrape trainer:

    real library  ->  review UI (play every sound)  ->  MY labels (ground truth)

Each sound gets:
  * role      - what the sound DOES on the timeline (transition / reaction / impact / ...)
  * reaction  - WHICH reaction it fits, when role == "reaction" (shock / cute / money / ...)
  * policy    - core (auto-usable) / topic_specific / meme_only / disabled
  * note      - free text

The result is written to soundeffects/sfx_labels.json (versioned). Nothing about the
live pipeline changes until the labels are ACTIVATED (see apply_labels()), so a review
session is always safe. sfx_library.build_library reads this file first once activated.

CLI:
    python -m tools.sfx_trainer status          # counts + what still needs a human ear
    python -m tools.sfx_trainer seed             # (re)write suggestions for unlabeled files
    python -m tools.sfx_trainer serve [--port N] # open the review UI
    python -m tools.sfx_trainer apply            # mark the saved labels ACTIVE for the pipeline
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SFX_DIR = ROOT / "soundeffects"
LABELS_PATH = SFX_DIR / "sfx_labels.json"
AUDIO_EXTS = {".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg"}
LABELS_VERSION = 1

# ---------------------------------------------------------------------------
# Taxonomy
# ---------------------------------------------------------------------------
# A sound's ROLE = what it does on the timeline. Only "reaction" sounds carry a
# specific reaction tag; the rest are placed by edit event (cut, emphasis, ...).
ROLES = [
    ("transition", "Transition", "whoosh / swish / slide / click played ON a cut or scene change"),
    ("reaction",   "Reaction",   "punctuates a moment - pick WHICH reaction below"),
    ("impact",     "Impact",     "hard hit / boom / punch on the hook + big reveals (dramatic beat)"),
    ("accent",     "Accent",     "small caption pop / tick / sparkle under a word or number"),
    ("ui",         "UI / click", "click / tap / keyboard / mouse - interface texture"),
    ("riser",      "Riser",      "tension build-up that swells INTO a reveal / big moment"),
    ("hook_riser", "Hook riser", "riser that builds through the HOOK and drops on the first cut"),
    ("skip",       "Skip / unused", "never place automatically (still draggable in the timeline)"),
]
ROLE_KEYS = [r[0] for r in ROLES]

# Reaction taxonomy. slug -> (label, examples). Editable in the UI (stored in the
# labels file under "reactions" so a rename/add survives).
REACTIONS = [
    ("shock_reveal",  "Shock / reveal",      "vine boom, braam, record-scratch WHAT"),
    ("comedy_fail",   "Comedy / fail / awkward", "fart, boing, frog, awkward pause"),
    ("sad_downer",    "Sad / downer",        "sad piano, downer, cartoon frown"),
    ("money_cash",    "Money / cash",        "cha-ching, coins, apple pay"),
    ("idea_reveal",   "Idea / magic reveal", "bright idea, magic sparkle, tada"),
    ("error_wrong",   "Error / wrong",       "wrong answer, buzzer, error bleep"),
    ("cute_aww",      "Cute / wholesome",    "aww, uwu, arigato, shiny pokemon"),
    ("suspense",      "Suspense / tension",  "sus violin, suspense strings, riser"),
    ("camera_photo",  "Camera / photo",      "shutter, camera flash"),
    ("celebrate",     "Celebrate / win",     "tada, applause, level-up, coin"),
    ("notification",  "Notification / msg",  "ding, imessage, message sent"),
    ("question",      "Question / confused", "huh, question mark"),
]
REACTION_KEYS = [r[0] for r in REACTIONS]
POLICIES = ["core", "topic_specific", "meme_only", "disabled"]


# ---------------------------------------------------------------------------
# Best-guess SEED. First matching rule wins. These are only SUGGESTIONS the user
# confirms/overrides by ear - deliberately generous so most files start non-empty.
# (substring in the lowercased stem, role, reaction_or_None, policy)
# ---------------------------------------------------------------------------
_SEED_EXACT = {
    # tricky ones the substring rules would mis-hit
    "fast-vine-boom-sound": ("reaction", "shock_reveal", "core"),
    "braam": ("reaction", "shock_reveal", "core"),
    "record-scratch-what": ("reaction", "shock_reveal", "core"),
    "record-scratch-clean": ("reaction", "shock_reveal", "core"),
    "death-bong": ("reaction", "death", "core"),
    "piano-dross": ("reaction", "sad_downer", "core"),
    "heavenly-musiic": ("reaction", "idea_reveal", "core"),
    "cartoon-strings": ("reaction", "suspense", "core"),
    "cartoon-frowning-sound": ("reaction", "sad_downer", "core"),
    "cartoon-boing-sfx": ("reaction", "comedy_fail", "core"),
    "apple-pay-sound": ("reaction", "money_cash", "topic_specific"),
    "shiny-pokemon": ("reaction", "cute_aww", "meme_only"),
    "taco-bell-bong": ("skip", None, "disabled"),
    "youtube-subscribe-sound-effects": ("reaction", "celebrate", "disabled"),
    "markel": ("skip", None, "disabled"),
    "fuuuuh": ("reaction", "comedy_fail", "meme_only"),
    "pause5": ("accent", None, "core"),
    "bleeep": ("reaction", "error_wrong", "core"),
    "throwing": ("transition", None, "core"),
    "laser-sound": ("accent", None, "core"),
    "sword-cut": ("reaction", "shock_reveal", "core"),
    "punch-effect": ("reaction", "shock_reveal", "core"),
    "slurpppppppp": ("reaction", "tasty", "meme_only"),
    "bike-horn": ("reaction", "comedy_fail", "meme_only"),
    "squeeky": ("reaction", "comedy_fail", "meme_only"),
    "prison-cell-door-lock": ("skip", None, "topic_specific"),
    "door-bell-ii": ("skip", None, "topic_specific"),
    "service-bell": ("accent", None, "topic_specific"),
    "typing-sound-efffect": ("skip", None, "topic_specific"),
    "timer-tick": ("accent", None, "topic_specific"),
    "check-mark": ("accent", None, "core"),
    "frog-croak": ("reaction", "comedy_fail", "meme_only"),
    "paper-ripping": ("skip", None, "core"),
    "snap": ("accent", None, "core"),
    "snap-sound": ("accent", None, "core"),
    "flip-fx": ("transition", None, "core"),
    "blink": ("accent", None, "core"),
    "blinky": ("accent", None, "core"),
    "question-mark": ("reaction", "question", "core"),
    "huh-sound": ("reaction", "question", "meme_only"),
}
_SEED_RULES = [
    # transitions
    ("whoosh", "transition", None, "core"), ("woosh", "transition", None, "core"),
    ("whosh", "transition", None, "core"), ("wosh", "transition", None, "core"),
    ("swoosh", "transition", None, "core"),
    ("swish", "transition", None, "core"), ("swipe", "transition", None, "core"),
    ("transicion", "transition", None, "core"), ("transition", "transition", None, "core"),
    ("riser", "riser", None, "core"),
    # reactions by theme
    ("cha-ching", "reaction", "money_cash", "core"), ("cha ching", "reaction", "money_cash", "core"),
    ("money", "reaction", "money_cash", "core"), ("coin", "reaction", "money_cash", "core"),
    ("magic", "reaction", "idea_reveal", "core"), ("idea", "reaction", "idea_reveal", "core"),
    ("reveal", "reaction", "idea_reveal", "core"), ("tada", "reaction", "celebrate", "core"),
    ("sad", "reaction", "sad_downer", "core"), ("aww", "reaction", "cute_aww", "core"),
    ("uwu", "reaction", "cute_aww", "core"), ("arigato", "reaction", "cute_aww", "core"),
    ("cute", "reaction", "cute_aww", "core"),
    ("fart", "reaction", "comedy_fail", "meme_only"), ("boing", "reaction", "comedy_fail", "core"),
    ("awkward", "reaction", "comedy_fail", "core"),
    ("sus", "reaction", "suspense", "core"), ("suspicious", "reaction", "suspense", "core"),
    ("suspense", "reaction", "suspense", "core"), ("violin", "reaction", "suspense", "core"),
    ("wrong", "reaction", "error_wrong", "core"), ("error", "reaction", "error_wrong", "core"),
    ("question", "reaction", "question", "core"), ("huh", "reaction", "question", "meme_only"),
    # camera
    ("shutter", "reaction", "camera_photo", "core"), ("camera", "reaction", "camera_photo", "core"),
    ("camara", "reaction", "camera_photo", "core"),
    # notification / messages
    ("imessage", "reaction", "notification", "topic_specific"),
    ("message", "reaction", "notification", "topic_specific"),
    ("subscribe", "reaction", "celebrate", "disabled"),
    # heavy hits for the hook + big reveals
    ("impact", "impact", None, "core"), ("boom", "impact", None, "core"),
    ("punch", "impact", None, "core"), ("slam", "impact", None, "core"),
    # accents / pops
    ("bubble", "accent", None, "core"), ("pop", "accent", None, "core"),
    ("ding", "reaction", "notification", "core"), ("bell", "accent", None, "core"),
    ("bright", "reaction", "idea_reveal", "core"),
    # ui / clicks
    ("click", "ui", None, "core"), ("mouse", "ui", None, "core"),
    ("ui-", "ui", None, "core"), ("tick", "accent", None, "core"),
    ("meme", "ui", None, "meme_only"),
]


def seed_label(stem):
    """Return a suggested {role, reaction, policy} for a filename stem (no extension)."""
    s = stem.lower()
    if s in _SEED_EXACT:
        role, reaction, policy = _SEED_EXACT[s]
        return {"role": role, "reaction": reaction, "policy": policy, "suggested": True}
    for token, role, reaction, policy in _SEED_RULES:
        if token in s:
            return {"role": role, "reaction": reaction, "policy": policy, "suggested": True}
    return {"role": "reaction", "reaction": None, "policy": "core", "suggested": True}


# ---------------------------------------------------------------------------
# Library scan + label store
# ---------------------------------------------------------------------------
def scan_files():
    """All top-level sound files in soundeffects/ (skips shorts_ready/, generated/)."""
    out = []
    if not SFX_DIR.exists():
        return out
    for p in sorted(SFX_DIR.iterdir()):
        if p.is_file() and p.suffix.lower() in AUDIO_EXTS:
            out.append(p)
    return out


def _ffprobe():
    for name in ("ffprobe", "ffprobe.exe"):
        try:
            subprocess.run([name, "-version"], capture_output=True, timeout=10)
            return name
        except Exception:
            continue
    return None


def duration_of(path, ffprobe=None):
    ffprobe = ffprobe or _ffprobe()
    if not ffprobe:
        return 0.0
    try:
        r = subprocess.run([ffprobe, "-v", "error", "-show_entries", "format=duration",
                            "-of", "default=nokey=1:noprint_wrappers=1", str(path)],
                           capture_output=True, text=True, timeout=20)
        return round(float((r.stdout or "0").strip() or 0.0), 3)
    except Exception:
        return 0.0


def load_labels():
    """Read the ground-truth store. Returns a dict with keys: version, active, reactions, labels."""
    if LABELS_PATH.exists():
        try:
            data = json.loads(LABELS_PATH.read_text(encoding="utf-8"))
            data.setdefault("version", LABELS_VERSION)
            data.setdefault("active", False)
            data.setdefault("reactions", [list(r) for r in REACTIONS])
            data.setdefault("labels", {})
            return data
        except Exception:
            pass
    return {"version": LABELS_VERSION, "active": False,
            "reactions": [list(r) for r in REACTIONS], "labels": {}}


def save_labels(data):
    data["version"] = LABELS_VERSION
    data["updated"] = int(time.time())
    SFX_DIR.mkdir(parents=True, exist_ok=True)
    LABELS_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return LABELS_PATH


def _rec_roles(rec):
    """A sound may sit in MULTIPLE roles. Read the new list form, falling back to the old
    single 'role' scalar (backward compat)."""
    roles = rec.get("roles")
    if roles is None:
        one = rec.get("role")
        roles = [one] if one else []
    return [r for r in roles if r]


def _rec_reactions(rec):
    rx = rec.get("reactions")
    if rx is None:
        one = rec.get("reaction")
        rx = [one] if one else []
    return [r for r in rx if r]


def merged_view(with_duration=False):
    """Per-file view: the SAVED human label if present, else the SEED suggestion.
    Returns (rows, meta) where each row = {file, stem, roles[], reactions[], policy, note,
    suggested(bool), labeled(bool), duration}. A sound can carry several roles/reactions."""
    data = load_labels()
    labels = data.get("labels", {})
    ffprobe = _ffprobe() if with_duration else None
    rows = []
    for p in scan_files():
        fn = p.name
        rec = labels.get(fn)
        if rec:
            roles = _rec_roles(rec)
            row = {"file": fn, "stem": p.stem, "roles": roles, "reactions": _rec_reactions(rec),
                   "policy": rec.get("policy", "core"), "note": rec.get("note", ""),
                   "suggested": False, "labeled": bool(roles)}
        else:
            s = seed_label(p.stem)
            row = {"file": fn, "stem": p.stem, "roles": [s["role"]],
                   "reactions": [s["reaction"]] if s["reaction"] else [],
                   "policy": s["policy"], "note": "", "suggested": True, "labeled": False}
        row["duration"] = duration_of(p, ffprobe) if with_duration else 0.0
        rows.append(row)
    return rows, data


def stats():
    rows, data = merged_view()
    by_role = {}
    by_reaction = {}
    labeled = 0
    for r in rows:
        for role in r["roles"]:                       # a multi-role sound counts in EACH
            by_role[role] = by_role.get(role, 0) + 1
        for rx in r["reactions"]:
            by_reaction[rx] = by_reaction.get(rx, 0) + 1
        if r["labeled"]:
            labeled += 1
    return {"total": len(rows), "labeled": labeled, "unlabeled": len(rows) - labeled,
            "active": data.get("active", False), "by_role": by_role, "by_reaction": by_reaction}


def apply_labels():
    """Mark the saved labels ACTIVE so sfx_library.build_library uses them. Requires that
    every file is human-labeled (no lingering suggestions)."""
    data = load_labels()
    rows, _ = merged_view()
    missing = [r["file"] for r in rows if not r["labeled"]]
    if missing:
        raise SystemExit(f"Cannot activate: {len(missing)} sound(s) still only have a suggestion "
                         f"(review them first). e.g. {', '.join(missing[:5])}")
    data["active"] = True
    save_labels(data)
    return data


def _cli(argv):
    cmd = (argv[0] if argv else "status").lower()
    if cmd == "status":
        st = stats()
        print(f"SFX library: {st['total']} sounds | labeled {st['labeled']} | "
              f"unlabeled {st['unlabeled']} | active={st['active']}")
        print("by role:", json.dumps(st["by_role"]))
        print("by reaction:", json.dumps(st["by_reaction"]))
    elif cmd == "seed":
        data = load_labels()
        rows, _ = merged_view()
        added = 0
        for r in rows:
            if not r["labeled"]:
                data["labels"][r["file"]] = {"roles": r["roles"], "reactions": r["reactions"],
                                             "policy": r["policy"], "note": "", "seeded": True}
                added += 1
        save_labels(data)
        print(f"Seeded {added} suggestion(s) into {LABELS_PATH}")
    elif cmd == "apply":
        apply_labels()
        print("Labels ACTIVATED - the SFX pipeline will now use them.")
    elif cmd == "serve":
        port = 7871
        if "--port" in argv:
            port = int(argv[argv.index("--port") + 1])
        from tools import sfx_trainer_serve
        sfx_trainer_serve.serve(port=port)
    else:
        print(__doc__)


if __name__ == "__main__":
    _cli(sys.argv[1:])
