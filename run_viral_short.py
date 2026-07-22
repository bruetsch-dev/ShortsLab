import json
import time
import traceback
import sys
sys.stdout.reconfigure(encoding='utf-8')
import agent_core

SCRIPT = (
    "Did you know that every year in Japan, thousands of people intentionally vanish without a trace? "
    "They are called the Jouhatsu, or the evaporated people. "
    "When debt, shame, or family pressure becomes too much, they don't just run away. "
    "They hire specialized night moving companies that pack up their entire lives under the cover of darkness. "
    "By morning, their apartments are completely empty. "
    "They assume a new secret life in another city, completely disconnected from their past."
)

# Base = the loaded scrape preset (ui_state.json)
with open("ui_state.json", encoding="utf-8") as f:
    form = json.load(f)

# Update form with native pipeline parameters
form.update({
    "script": SCRIPT,
    "title": "Jouhatsu - The Evaporated People of Japan",
    "slug": "jouhatsu_viral_short",
    "ui_form": "1",                       # behave like a real UI submit
    "generate_voice": "on",
    "clip_source": "scrape",
    "scraping_engine": "v2",              # Ensure v2 scraper is used
    "scrape_platforms": "tiktok,x,instagram",
    "scrape_terms": "japan missing persons, tokyo dark alleys, japanese night streets",
    "script_relevancy": "80",             # strict relevance check
    "vfx_amount": "high",                 # force dense visual editing (arrows/reactions)
    "sfx_amount": "high",                 # force dense SFX
    "add_visual_effects": True,           # Enable visual agent
    "out_sfx": True,
    "out_transition_sfx": True,
    "out_captions": True,
    "use_llm_video_review": "",           # skip double-render LLM review for speed in this run
    "open_timeline_no_render": False,     # FORCE THE FINAL RENDER
})

t0 = time.time()
def cb(m):
    # Print status log
    print(f"[{time.time()-t0:6.1f}s] {m}", flush=True)

print("=== STARTING NATIVE PIPELINE ===", flush=True)
try:
    result = agent_core.run_project(form, cb)
    print("=== FINAL RESULT ===", flush=True)
    print(json.dumps(result, indent=2, default=str), flush=True)
except Exception as exc:
    print("=== PIPELINE ERROR ===", flush=True)
    traceback.print_exc()
