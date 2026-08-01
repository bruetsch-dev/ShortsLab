import json
import time
import sys
sys.stdout.reconfigure(encoding='utf-8')
import agent_core
import discovery_short

with open("ui_state.json", encoding="utf-8") as f:
    base_form = json.load(f)

# If args provided, use them as files to process. Otherwise use a default list.
files_to_process = sys.argv[1:] if len(sys.argv) > 1 else [
    r"D:\data\AutoShortsClaude\reference.mp4",
    r"D:\data\AutoShortsClaude\ref3.mp4",
    r"D:\data\AutoShortsClaude\ref4.mp4"
]

for i, filepath in enumerate(files_to_process):
    print(f"\n\n=== PROCESSING LOCAL FILE {i+1}/{len(files_to_process)}: {filepath} ===", flush=True)
    
    # 1. CLIP SHORT MODE (style="story")
    print("\n--- 1. CLIP SHORT MODE ---", flush=True)
    form_story = base_form.copy()
    form_story.update({
        "candidate_url": filepath,
        "gen_topic": "",
        "generate_voice": "on",
        "clip_source": "scrape",
        "scraping_engine": "v2",
        "open_timeline_no_render": False,
        "timeline_engine": False,
        "enable_speaker_hook": True, # Ensure influencer hook is enabled
    })
    
    def cb(m):
        print(f"[{time.time():.1f}] {m}", flush=True)
    
    try:
        discovery_short.run_discovery_short(form_story, cb, style="story")
    except Exception as exc:
        import traceback
        traceback.print_exc()

    # 2. FACT SHORT MODE (agent_core pipeline)
    print("\n--- 2. FACT SHORT MODE ---", flush=True)
    form_fact = base_form.copy()
    form_fact.update({
        "reference_video": filepath,  # Use as reference/source
        "clip_source": "scrape",
        "script": "Did you know that every year in Japan, thousands of people intentionally vanish without a trace? They are called the Jouhatsu, or the evaporated people. When debt, shame, or family pressure becomes too much, they don't just run away. They hire specialized night moving companies that pack up their entire lives under the cover of darkness. By morning, their apartments are completely empty. They assume a new secret life in another city, completely disconnected from their past.",
        "title": "Jouhatsu - The Evaporated People of Japan",
        "slug": f"jouhatsu_fact_{i}",
        "ui_form": "1",
        "generate_voice": "on",
        "enable_speaker_hook": True, # Ensure influencer hook is enabled
        "scrape_platforms": "tiktok,x,instagram",
        "scrape_terms": "japan missing persons, tokyo dark alleys",
        "script_relevancy": "80",
        "vfx_amount": "high",
        "sfx_amount": "high",
        "add_visual_effects": True,
        "out_sfx": True,
        "out_transition_sfx": True,
        "out_captions": True,
        "open_timeline_no_render": False,
    })
    
    try:
        agent_core.run_project(form_fact, cb)
    except Exception as exc:
        import traceback
        traceback.print_exc()

print("\n=== ALL FILES FINISHED ===", flush=True)
