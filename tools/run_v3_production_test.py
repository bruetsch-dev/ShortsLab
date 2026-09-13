"""Run one standalone Clip Short V3 production test without restarting the desktop app."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import agent_core


SCRIPT = """Japan's convenience stores can rescue a bad day in three moves.
First, the hot-food shelf: onigiri, bento, fried chicken and noodles are waiting when you miss dinner.
Second, the coffee machine. Commuters grab an iced coffee, pay in seconds, and run for the train.
And third, the counter does things that do not feel like convenience-store jobs at all.
You can send a parcel, pay a bill, collect an online order, or use the copier beside the snacks.
That is why a tiny konbini feels less like a shop and more like a daily emergency button."""

FIELDS = {
    "title": "Why Japanese Convenience Stores Feel Like Cheat Codes V3 Test",
    "script": SCRIPT,
    "clip_source": "scrape",
    "scraping_engine": "v3",
    "scrape_platforms": "tiktok,x,instagram",
    # Seconds, matching the app's own slider (1800-7200, default 3600). The harness used to sit at
    # 600 - a sixth of what a real run gets - so round 1 consumed the whole budget and the
    # adaptive round 2 never issued a single search.
    "scrape_time_budget": "3600",
    "scrape_sort": "ALL",
    "script_relevancy": "80",
    "reasoning_model": "openai/gpt-5.6-luna",
    "tts_model": "flash",
    "tts_voice": "Laomedeia",
    "tts_native_speed": "1.125",
    "out_video_clips": True,
    "out_captions": True,
    "out_sfx": False,
    "out_transition_sfx": False,
    "out_background_music": False,
    "background_music_enabled": False,
    "influencer_hook": False,
    "halt_after_speech": False,
    "allow_gpt": False,
    "allow_seedance": False,
}


def status(message):
    print(message, flush=True)


if __name__ == "__main__":
    print("TEST_SCRIPT|" + SCRIPT.replace("\n", " "), flush=True)
    result = agent_core.run_project(FIELDS, status)
    print("TEST_RESULT|" + repr(result), flush=True)
