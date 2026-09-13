"""Run one auditable, no-voice V4 sourcing smoke test.

This helper is deliberately small: it lets us prove the scraper's discovery
and editorial gates without spending a TTS call.  It always writes an outcome
file, including a traceback, so a stopped terminal can never masquerade as a
successful sourcing pass.
"""
from __future__ import annotations

import json
import os
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import scrape_v4


def main() -> int:
    project = Path(sys.argv[1]).resolve()
    title = "Japan's hot drinks vending machines"
    scenes = [
        {
            "exact_voice_text": "In Japan, a vending machine can hand you a hot drink on a freezing night.",
            "visual_subject": "Japanese vending machine hot canned coffee winter night",
            "visual_action": "person presses hot drink button and receives a steaming can",
            "search_queries": [
                "Japan vending machine hot canned coffee",
                "Japanese vending machine hot drink winter",
            ],
        },
        {
            "exact_voice_text": "The same machine can switch its rows from cold blue labels to warm red labels.",
            "visual_subject": "Japanese vending machine red hot labels blue cold labels",
            "visual_action": "close-up choosing a red hot beverage button",
            "search_queries": [
                "Japan vending machine red hot drink button close up",
                "Japanese vending machine hot cold labels",
            ],
        },
        {
            "exact_voice_text": "It is a tiny detail that makes a late walk home feel surprisingly thoughtful.",
            "visual_subject": "person walking Japanese street at night holding hot canned coffee",
            "visual_action": "night walk through Japan with warm drink in hand",
            "search_queries": [
                "Japan night walk holding canned coffee",
                "Japanese street night hot drink in hand",
            ],
        },
    ]
    # A second, deliberately different factual script keeps live verification
    # honest: a scraper that succeeds only for one hand-tuned query set is not
    # a production scraper.  Select it with V4_SMOKE_PRESET=station.
    if os.environ.get("V4_SMOKE_PRESET", "").strip().casefold() == "station":
        title = "Small details at Japanese train stations"
        scenes = [
            {
                "exact_voice_text": "At many Japanese stations, the platform melody is more than background noise.",
                "visual_subject": "Japanese train station platform departure melody",
                "visual_action": "train arrives at a Japanese platform as a station chime plays",
                "search_queries": [
                    "Japan train station arrival platform", "Japanese station departure melody",
                ],
            },
            {
                "exact_voice_text": "The yellow tactile path guides passengers through even the busiest platforms.",
                "visual_subject": "Japanese train station yellow tactile paving platform walking",
                "visual_action": "passengers walk beside yellow tactile paving toward a train",
                "search_queries": [
                    "Japan train station yellow tactile paving", "Japanese platform passengers walking train",
                ],
            },
            {
                "exact_voice_text": "And when the doors close, everyone lines up so precisely that the rush stays calm.",
                "visual_subject": "Japanese commuters queue train doors platform",
                "visual_action": "commuters queue and board a Japanese train in orderly lines",
                "search_queries": [
                    "Japan commuters queue train doors", "Japanese train boarding platform line",
                ],
            },
        ]
    if os.environ.get("V4_SMOKE_PRESET", "").strip().casefold() == "ramen":
        title = "Why tiny ramen shops feel so different in Japan"
        scenes = [
            {
                "exact_voice_text": "In many Japanese ramen shops, you choose your bowl from a ticket machine before you even sit down.",
                "visual_subject": "Japanese ramen shop ticket vending machine",
                "visual_action": "customer presses a ramen ticket machine button and receives a meal ticket",
                "search_queries": [
                    "Japan ramen ticket machine", "Japanese ramen shop vending machine order",
                ],
            },
            {
                "exact_voice_text": "Behind the counter, the chef builds each bowl in full view, from boiling noodles to the final topping.",
                "visual_subject": "Japanese ramen chef making noodles bowl counter",
                "visual_action": "ramen chef lifts noodles and finishes a bowl behind counter",
                "search_queries": [
                    "Japan ramen chef making bowl", "Japanese ramen noodles counter chef",
                ],
            },
            {
                "exact_voice_text": "Then strangers sit shoulder to shoulder, eat quickly, and make room for the next person in line.",
                "visual_subject": "Japanese ramen shop counter customers eating",
                "visual_action": "customers eat ramen at a narrow Japanese counter then leave",
                "search_queries": [
                    "Japan ramen shop counter customers eating", "Japanese tiny ramen shop counter",
                ],
            },
        ]
    if os.environ.get("V4_SMOKE_PRESET", "").strip().casefold() == "school_lunch":
        title = "Why Japanese school lunches feel unusually organized"
        scenes = [
            {
                "exact_voice_text": "At many Japanese schools, students do not just eat lunch. They help run it.",
                "visual_subject": "Japanese school students serve lunch classroom",
                "visual_action": "students in a Japanese classroom hand out school lunch trays",
                "search_queries": [
                    "Japan school lunch students serving classroom",
                    "Japanese school kyushoku lunch serving",
                ],
            },
            {
                "exact_voice_text": "Instead of a cafeteria line, classmates bring trays, soup and rice directly to every desk.",
                "visual_subject": "Japanese school lunch tray soup rice desks",
                "visual_action": "students carry lunch trays and serve rice in Japanese classroom",
                "search_queries": [
                    "Japan school lunch trays classroom rice",
                    "Japanese kyushoku classroom lunch serving",
                ],
            },
            {
                "exact_voice_text": "Then they eat the same meal together before packing everything away themselves.",
                "visual_subject": "Japanese students eating school lunch classroom cleanup",
                "visual_action": "Japanese students eat lunch at desks then clear lunch trays",
                "search_queries": [
                    "Japanese students eating school lunch classroom",
                    "Japan school lunch cleanup trays",
                ],
            },
        ]
    if os.environ.get("V4_SMOKE_PRESET", "").strip().casefold() == "school_cleaning":
        title = "Why Japanese students clean their own schools"
        scenes = [
            {
                "exact_voice_text": "In many Japanese schools, students pick up brooms instead of waiting for a janitor.",
                "visual_subject": "Japanese students cleaning classroom with brooms",
                "visual_action": "students sweep desks and classroom floor in Japan",
                "search_queries": [
                    "Japanese students clean classroom brooms",
                    "日本 学校 掃除 生徒 教室",
                ],
            },
            {
                "exact_voice_text": "Groups move through the halls with mops, cloths and dustpans after class.",
                "visual_subject": "Japanese students mopping school hallway",
                "visual_action": "students mop a Japanese school corridor together",
                "search_queries": [
                    "Japanese school students mop hallway",
                    "学校 清掃 廊下 モップ 生徒",
                ],
            },
            {
                "exact_voice_text": "The point is not perfect cleaning. It is learning that the space belongs to everyone.",
                "visual_subject": "Japanese students wipe school floor teamwork",
                "visual_action": "students wipe a classroom floor together in Japan",
                "search_queries": [
                    "Japanese students wipe classroom floor together",
                    "日本 学校 床 拭き掃除 生徒",
                ],
            },
        ]
    project.mkdir(parents=True, exist_ok=True)
    (project / "input").mkdir(exist_ok=True)
    (project / "input" / "script.txt").write_text("\n".join(x["exact_voice_text"] for x in scenes), encoding="utf-8")
    log = []
    live_log = project / "review" / "v4_live_log.txt"
    live_log.parent.mkdir(exist_ok=True)

    def status(message):
        # Keep an on-disk heartbeat.  Bright jobs can legitimately take many
        # minutes and a detached terminal must not make a live run look dead.
        line = f"{__import__('datetime').datetime.now().isoformat(timespec='seconds')}  {message}"
        # Windows launches this helper under a legacy cp1252 console in some
        # Codex sessions.  The audit itself must retain native Japanese terms,
        # but logging them must never abort a paid Bright result batch.
        try:
            print(message, flush=True)
        except UnicodeEncodeError:
            encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
            print(str(message).encode(encoding, errors="backslashreplace").decode(encoding), flush=True)
        log.append(str(message))
        with live_log.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    try:
        # Keep the normal test Bright-first.  Setting this false proves that a
        # healthy Bright run never silently turns the user's TikTok session
        # into a second keyword-search scraper; the session is still used for
        # the selected post's media delivery.
        config = {"title": title,
                  "v4_tiktok_login_fallback": os.environ.get("V4_SMOKE_BRIGHT_ONLY") != "1"}
        if os.environ.get("V4_SMOKE_LOGIN_RECOVERY") == "1":
            # Exercise the already-authorized recovery route without buying a
            # duplicate Bright dataset batch. A normal production run still
            # uses Bright first and only reaches this route after a real error.
            config.update({"bright_unlocker_zone": "__local_recovery_test__", "v4_instagram_enabled": False})
        sourced, clips = scrape_v4.scrape_social_plan_v4(
            config, scenes, project, per_clip_seconds=2.3,
            status_cb=status,
        )
        outcome = {"ok": True, "clips": clips, "scenes": sourced, "log": log}
        code = 0
    except Exception as exc:  # outcome must survive a failed live test
        outcome = {"ok": False, "error": repr(exc), "traceback": traceback.format_exc(), "log": log}
        code = 1
    review = project / "review"
    review.mkdir(exist_ok=True)
    (review / "v4_smoke_outcome.json").write_text(json.dumps(outcome, ensure_ascii=False, indent=2), encoding="utf-8")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
