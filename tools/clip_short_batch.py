"""Run Clip Shorts back to back, each on a FRESH script, and deliver the finished file.

One topic per run, never repeated, so every pass exercises different search terms, different
chapter shapes and different footage - which is the point: a script that already has cached clips
proves nothing about the pipeline.

Each run: build -> render the timeline the pipeline leaves behind -> copy the MP4 to the delivery
folder -> append a one-line verdict to the log. A failure is recorded and the batch moves on.

    python tools/clip_short_batch.py [--only SLUG] [--count N]
"""
import json
import os
import shutil
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
# ProPainter is gated behind this variable in the current working tree (an uncommitted change:
# HEAD reads `if allow_gpu and propainter_available()`), and nothing sets it - so every caption
# removal silently fell back to per-frame OpenCV inpainting. Set before agent_core is imported.
os.environ.setdefault("CAPTION_REMOVER_PROPAINTER", "1")

import agent_core

DELIVERY = Path("D:/iphone 2")   # the user's existing folder - NOT "i phone 2", which I created by mistake
LEDGER = Path(__file__).resolve().parents[1] / "review" / "clip_short_batch.json"

# Every entry is a self-contained ~30 second Short. They are deliberately spread across concrete,
# filmable subjects: a chapter can only be proven by footage that exists, so an abstract topic
# tests the writer rather than the scraper.
SCRIPTS = [
    ("vending_machines", "Japan Has A Vending Machine For Everything", """
Japan has roughly one vending machine for every thirty people, and they are not all drinks.
There are machines for hot ramen, for fresh eggs, for umbrellas when it starts raining.
Some sell frozen gyoza on a residential street with nobody around for hours.
They survive outside all night because street crime is low enough that nobody bothers them.
And they are refilled by hand, one row at a time, by someone who drives a route every morning."""),
    ("train_punctual", "Japanese Trains Apologise For Twenty Seconds", """
A Japanese train line once issued a public apology for leaving twenty seconds early.
Not late. Early. The doors closed before the timetable said they should.
Average delay on the Tokaido Shinkansen is measured in seconds, not minutes.
Cleaning crews turn a whole train around in seven minutes, seats rotated, floors done.
And if a train is more than five minutes late, staff hand out delay certificates at the gate."""),
    ("konbini_night", "What A Japanese Convenience Store Looks Like At 3AM", """
At three in the morning a Japanese convenience store is fully lit and completely staffed.
One person is restocking the hot case while the shelves are being faced label-forward.
The fried chicken counter is still running. The coffee machine still works.
Deliveries arrive in the dark, three times a day, so nothing on the shelf is old.
It is the same store at three as at noon, and that is the whole point of it."""),
    ("school_cleaning", "Japanese Students Clean Their Own School", """
There are no cleaners in most Japanese schools. The students do it themselves.
Every day, for about fifteen minutes, classrooms and corridors get swept and wiped by the kids.
They move the desks, they do the toilets, they take out the rubbish in teams on a rota.
The idea is not to save money. It is that you take care of a place you have to clean.
By the time they leave school, nobody has to be told to pick up after themselves."""),
    ("umbrella_lockers", "Japan Has Lockers Just For Wet Umbrellas", """
Outside a Japanese shop on a rainy day you will find a rack of small metal lockers.
They are for umbrellas. You take a key, lock yours in, and go inside dry-handed.
Some shops instead have a machine that wraps your umbrella in a thin plastic sleeve.
Both exist for the same reason: nobody wants a wet floor, and nobody wants your drips.
It is a tiny piece of infrastructure for a problem most countries just live with."""),
    ("fake_food", "Japanese Restaurants Display Plastic Food", """
Outside a Japanese restaurant the window is full of meals that will never be eaten.
They are plastic, hand-painted, and often cost more than the dish they are advertising.
A craftsman builds each one by hand, pouring resin and painting the sauce on by brush.
The noodles get a wire lifted mid-air so the bowl looks like someone just picked it up.
You point at the window, and the language barrier is gone."""),
    ("train_pushers", "Tokyo Employs People To Push You Onto Trains", """
On a Tokyo platform at rush hour there are staff whose job is to push people into the carriage.
They wear white gloves and they are unfailingly polite about it.
The trains run at well over a hundred percent of their rated capacity in the morning.
Nobody complains, because the alternative is waiting for a train that will be just as full.
And two stops later the same crowd unloads in about fifteen seconds."""),
    ("capsule_hotel", "A Capsule Hotel Is Smaller Than You Think", """
A capsule in a Japanese capsule hotel is about the size of a single bed with a lid.
You get a mattress, a light, a socket and a small screen, and that is the whole room.
Your shoes go in a locker at the door. Your bag goes in a second locker down the corridor.
The bathroom is shared, the lounge is shared, and everyone is quiet because everyone can hear.
People use them after missing the last train, which in Tokyo happens to somebody every night."""),
    # Round two, written after the first API-mode delivery: every topic here is something people
    # PHYSICALLY FILM constantly - machines being used, food being made, crowds moving. The first
    # list's weakest performers (umbrella lockers, capsule interiors) failed not on writing but
    # on footage that barely exists as UGC.
    ("conveyor_sushi", "Sushi That Finds Your Table By Itself", """
In a Japanese conveyor sushi shop you order on a screen and the food drives itself to you.
A little express tray shoots down a second track and stops exactly at your seat.
You take your plate, tap a button, and the tray whirs back to the kitchen.
Finished plates drop into a slot at the table, and the bill counts itself.
Five plates in, a slot machine on the screen might win you a capsule toy."""),
    ("ufo_catcher", "Japan's Claw Machines Are A Whole Economy", """
In a Japanese arcade the claw machines fill entire floors, and people play them seriously.
Staff will move the prize into a better spot if you ask, because they want you to win.
Regulars film their wins: a figure walked to the edge over fifteen careful drops.
Whole shops resell claw machine prizes to people who would rather pay than play.
And the machines are spotless, restocked all day like a supermarket shelf."""),
    ("ramen_ticket", "You Buy Ramen From A Machine Before You Sit Down", """
In many Japanese ramen shops you pay before you ever see a person.
A ticket machine by the door takes your money and prints a little slip.
You hand the slip over the counter, and that is the entire conversation.
The cook nods, and three minutes later the bowl lands in front of you.
No bill, no tipping, no ordering mistakes - the machine already settled everything."""),
    ("depachika", "The Food Basement Under Japanese Department Stores", """
Under a Japanese department store there is usually a whole floor of food called a depachika.
Glass cases of bento boxes arranged like jewellery, each one priced and labelled.
Staff call out timed discounts in the evening, and shoppers circle like it is a sport.
There are queues for single strawberries that cost more than a whole lunch.
And everything is wrapped so beautifully that opening it feels like a second gift."""),
    ("purikura", "Japan's Photo Booths Edit You Automatically", """
A purikura is a Japanese photo booth the size of a small room, and it edits you in real time.
The screen smooths your skin, widens your eyes and brightens everything before you even pose.
Groups squeeze in, follow the voice prompts, and hammer the touchscreen between shots.
Afterwards you decorate the photos at a second station with pens, stamps and sparkles.
The prints come out as tiny stickers, and everyone splits the sheet outside the booth."""),
    ("shinkansen_clean", "Japan Cleans A Bullet Train In Seven Minutes", """
When a shinkansen reaches Tokyo, a cleaning crew is already waiting on the platform in a line.
They bow to the arriving train, then split up, one cleaner per car.
Seats get rotated to face the new direction with a single lever pull.
Tray tables wiped, floors swept, headrest covers swapped, all against a countdown.
Seven minutes later they line up again, bow, and the train boards as if it just left the factory."""),
]


def timeline_coverage(project_slug):
    """(beats carrying real footage, beats) for the finished project.

    The signal is the scene's own `assignment_type`: a beat the matcher could not cover is
    rewritten to `uncovered_still` and handed to the image generator. With image generation off -
    which is the Clip Short preset - that "still" is a text card reading SCENE 01, and thirty of
    them in a row rendered, passed blackdetect, and were filed as a delivered short.
    """
    path = agent_core.PROJECTS_DIR / project_slug / "config" / "project.json"
    try:
        scenes = json.loads(path.read_text(encoding="utf-8")).get("scenes") or []
    except (OSError, ValueError):
        return 0, 0
    real = sum(1 for s in scenes
               if s.get("asset") and str(s.get("assignment_type") or "") != "uncovered_still")
    return real, len(scenes)


def load_ledger():
    try:
        return json.loads(LEDGER.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"runs": []}


def save_ledger(data):
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    LEDGER.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")


def fields_for(title, script):
    return {
        "title": title,
        "script": script.strip(),
        "clip_source": "scrape",
        # V4, like the app and like run_project's own default. This said "v3", so every
        # batch run - and every run started through this builder, including the ones meant
        # to exercise V4 - went to the previous engine: none of the V4 gates applied, no
        # editorial contract reached the search, and the run looked like a V4 run in the
        # log. agent_core says it in its own comment and then this file overrode it.
        "scraping_engine": "v4",
        "scrape_platforms": "tiktok,x,instagram",
        "scrape_time_budget": "3600",
        "scrape_sort": "ALL",
        "script_relevancy": "80",
        "reasoning_model": "openai/gpt-5.6-luna",
        # Laomedeia on Gemini. tts_native_speed is deliberately NOT sent: it is a Seed-only
        # parameter, and two runs that carried it came out on bytedance/seed-speech with
        # stokie_en instead. A Clip Short's pace comes from agent_core's own SCRAPE_VOICE_SPEED.
        "tts_model": "flash",
        "tts_voice": "Laomedeia",
        "out_video_clips": True,
        "out_captions": True,
        # The app's own Clip Short preset: reaction SFX off, TRANSITION SFX ON. Sending both as
        # False made agent_core compute sfx_enabled = (out_sfx or out_tr_sfx) = False, which
        # skips place_editor_sfx entirely - and the HOOK RISER lives in that function. That is
        # why the delivered Shorts had neither transition sounds nor a riser.
        "out_sfx": False,
        "out_transition_sfx": True,
        "out_background_music": False,
        "background_music_enabled": False,
        "influencer_hook": False,
        "halt_after_speech": False,
        "allow_gpt": False,
        "allow_seedance": False,
    }


def run_one(slug, title, script):
    log = lambda message: print(message, flush=True)
    started = time.time()
    entry = {"slug": slug, "title": title, "started": time.strftime("%Y-%m-%d %H:%M:%S")}
    try:
        result = agent_core.run_project(fields_for(title, script), log)
        project_slug = str((result or {}).get("project_slug") or "")
        entry["project_slug"] = project_slug
        if not project_slug:
            raise RuntimeError("the run returned no project slug")
        # The pipeline deliberately stops at the timeline editor; a batch has to take that last
        # step itself or it produces nothing watchable.
        edits_path = agent_core.PROJECTS_DIR / project_slug / "config" / "timeline_edits.json"
        try:
            edits = json.loads(edits_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            edits = {}
        # Prove the narrator actually used is the one asked for. Two earlier runs quietly came
        # out on a different engine and voice, and only a hand-grep of the log found it.
        voice_used = ""
        try:
            cfg = json.loads((agent_core.PROJECTS_DIR / project_slug / "config" /
                              "project.json").read_text(encoding="utf-8"))
            voice_used = str(cfg.get("tts_voice") or cfg.get("voice") or "")
        except (OSError, ValueError):
            pass
        entry["voice"] = voice_used
        if voice_used and voice_used != "Laomedeia":
            log(f"VOICE|{slug}: asked for Laomedeia, got {voice_used!r}")
        rendered = agent_core.render_project_timeline(project_slug, edits, status_cb=log)
        video = Path(str((rendered or {}).get("video") or ""))
        if not video.is_file():
            raise RuntimeError("the render produced no file")
        # Last line of defence, the same one the longform path has always had: prove the file
        # has pixels. A delivered Short went black for half a second and only a hand-sampled
        # contact sheet caught it. Recorded rather than hidden - the file is still watchable,
        # but a defect nobody wrote down is a defect nobody fixes.
        defects = []
        quality = (rendered or {}).get("editorial_quality") or {"status": "unverified"}
        entry["editorial_quality"] = quality
        entry["quality_passed"] = quality.get("status") == "passed"
        if not entry["quality_passed"]:
            defects.append(f"editorial quality: {quality.get('status', 'unverified')}")
        # A short with no footage in it is not a short. One run came back with 11 beats and 0
        # clips, rendered thirty placeholder cards reading "SCENE 01", and was filed here as
        # delivered with no defects because blackdetect does not fire on a dark grey card.
        # Coverage is counted from the timeline the pipeline actually rendered.
        covered, total = timeline_coverage(project_slug)
        if total and not covered:
            raise RuntimeError(f"no footage at all: 0 of {total} beats have a clip - the render "
                               f"is placeholder cards, not a short")
        if total and covered < total:
            defects.append(f"{total - covered} of {total} beats have no footage "
                           f"(marked in the timeline editor)")
        try:
            import longform_video
            black = longform_video.detect_black_segments(video, min_duration=0.12)
            if black:
                spans = ", ".join(f"{b['start']:.2f}-{b['end']:.2f}s" for b in black[:4])
                defects.append(f"black frames at {spans}")
        except Exception as exc:                                        # noqa: BLE001
            defects.append(f"black-frame check failed ({type(exc).__name__})")
        DELIVERY.mkdir(parents=True, exist_ok=True)
        target = DELIVERY / f"clip_{slug}.mp4"
        shutil.copy2(video, target)
        quality_report = video.with_suffix(".editorial.json")
        if quality_report.is_file():
            shutil.copy2(quality_report, target.with_suffix(".editorial.json"))
        entry.update(ok=True, video=str(target), seconds=round(time.time() - started, 1),
                     defects=defects)
        for note in defects:
            log(f"DEFECT|{slug}: {note}")
        log(f"DELIVERED|{target}")
    except Exception as exc:                                            # noqa: BLE001
        entry.update(ok=False, error=f"{type(exc).__name__}: {exc}",
                     seconds=round(time.time() - started, 1))
        log("FAILED|" + entry["error"])
        traceback.print_exc()
    return entry


def main(only, count):
    ledger = load_ledger()
    done = {row.get("slug") for row in ledger["runs"] if row.get("ok")}
    # A topic the platforms simply do not carry fails the same way every time. Re-queueing it
    # burns twenty minutes per attempt and crowds out scripts that can actually be tested.
    done |= {row.get("slug") for row in ledger["runs"]
             if "no footage at all" in str(row.get("error") or "")}
    queue = [row for row in SCRIPTS if (only and row[0] == only) or (not only and row[0] not in done)]
    for slug, title, script in queue[:count]:
        print(f"=== CLIP SHORT: {slug} - {title} ===", flush=True)
        ledger["runs"].append(run_one(slug, title, script))
        save_ledger(ledger)
    print("BATCH_DONE|" + json.dumps(
        {"ok": sum(1 for r in ledger["runs"] if r.get("ok")),
         "failed": sum(1 for r in ledger["runs"] if not r.get("ok"))}), flush=True)


if __name__ == "__main__":
    args = sys.argv[1:]
    only = args[args.index("--only") + 1] if "--only" in args else None
    count = int(args[args.index("--count") + 1]) if "--count" in args else 1
    main(only, count)
