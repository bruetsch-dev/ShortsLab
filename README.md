# Autonomous Shorts Agent

Local UI for creating 9:16 Shorts from a text script, optional uploaded speech audio, and optional visual direction.
By default, GPT-5.5 acts as an Auto Director and decides the media mix and agent steps.

## Start

For the clean app window, double-click:

```bat
start.bat
```

This opens the UI in browser app mode without URL bar, bookmark bar, or normal browser navigation.

For server-only startup, double-click:

```bat
run_app.bat
```

Then open:

```text
http://127.0.0.1:7865
```

## Project Folders

Every new project is created under:

```text
projects/<project_slug>/
```

with these folders:

```text
seedance 2.0/
gpt images/
web images/
input/
config/
renders/
review/
local media/
```

The Auto Director decides whether to use web images, GPT images, Seedance clips, audio timing, and GPT-5.5 review/correction for each run.
Use `Visual Ablauf / direction` when you want to guide the visual sequence separately from the spoken script. It can be plain visual notes or timed beats, for example maps first, then archive photos, then slow Seedance reconstruction.
The app maps that visual direction onto script scenes and uses it for Auto Director decisions, web-search planning, GPT Image prompts, Seedance prompts, render planning, and GPT-5.5 review.
The agent searches and downloads script-matching web images automatically into `web images/`.
By default it asks WaveSpeed GPT-5.5 to create a topic-aware search plan, then uses Wikipedia/Wikimedia Commons queries and relevance scoring to download better matches.
Each run targets a 10-15 approved web-image pool when automatic web images are enabled.
Before rendering, GPT-5.5 reviews the downloaded web-image contact sheet for script match, 9:16 crop viability, seriousness, and shot usefulness. Rejected images are moved to `web images/rejected/`, replacements are downloaded, and the final approved contact sheet is saved in `review/`.
During a running job, the right-hand media column shows individual web and static GPT image files. You can select images for replacement while Seedance is generating; queued replacements are processed after Seedance generation and before the final render.
`Static GPT images` means standalone still-image B-roll only. GPT source images for Seedance I2V are decided by the Seedance clip count and are added separately when needed.
GPT image prompts explicitly prefer one coherent cinematic scene and limit collage/document-board style outputs to rare cases where that is useful.
It saves source metadata in:

```text
web images/web_image_manifest.json
```

You can still add your own fallback images to `web images/` or `local media/` before running a project.

If `seedance 2.0/` already has enough MP4 clips for the planned motion scenes, the app reuses those clips and does not generate new Seedance videos.

When loading an old project, `Loaded project action` can be set to:

- `Recut existing media only`: no new web images, no new GPT images, no new Seedance clips.
- `New static GPT images only + recut`: generates only new standalone GPT stills, then recuts with existing media plus those images.
- `New web images only + recut`: downloads only new web images, then recuts with existing media plus those images.

After loading an old project, the main form shows individual web and static GPT image previews from that project. Select any of those images before starting the run to queue them for replacement. The queued replacements are processed after Seedance generation and before the final render.

All recut modes block new Seedance generation and write a new timestamped render instead of overwriting the old one.

## API Keys

The app reads the WaveSpeed key from `.env` or the environment:

```powershell
$env:WAVESPEED_API_KEY="..."
```

`WAVESPEED_API_KEY` is used for GPT images, Seedance 2.0, GPT-5.5 web-search planning, GPT-5.5 video review/correction, and Gemini 3.5 Flash audio timing.

Keys are not written into project config files.

## Current Agent Behavior

The agent:

- parses timed scripts or plain scripts into scenes
- maps the optional visual direction prompt onto those scenes
- asks GPT-5.5 to choose the WaveSpeed/API workflow, media counts, and per-scene visual strategy
- counts standalone static GPT images separately from GPT images used as Seedance I2V sources
- analyzes optional speech audio with Gemini 3.5 Flash to create transcript and timed visual beats
- estimates speaking time from the script word count when no audio timing is available
- uses GPT-5.5 planning to turn the script into better web image searches
- searches Wikipedia/Wikimedia Commons for web images that match each script scene
- downloads those web images into the project and records source/license metadata
- reviews and corrects the web-image pool with GPT-5.5 before any render starts
- shows individual web/static GPT media files in the job page and lets the user queue replacements before rendering
- creates contact sheets for downloaded/generated image sets and saves them in `review/`
- chooses a limited number of Seedance scenes for strong motion beats
- uses existing Seedance clips before generating anything
- generates new Seedance 2.0 clips with audio enabled
- submits GPT image and Seedance generation jobs in parallel with a conservative default concurrency of 2
- uses Seedance clip audio in the final render, mixed quietly under uploaded speech audio when speech is present
- chooses an instrumental background music loop from `background music/` and mixes it quietly under the Short
- adds subtle local SFX from `soundeffects/` for serious transitions, impacts, and foley moments
- creates GPT image prompts for missing motion-scene source images
- chooses local/web/GPT images as still B-roll for non-Seedance scenes
- avoids reusing Seedance source images as static B-roll
- renders a 1080x1920 MP4, with uploaded speech audio when provided
- creates scene-level and shot-level review sheets
- reviews those sheets with GPT-5.5 and can render corrected versions for framing/motion/readability issues
- runs two GPT-5.5 review passes whenever review is enabled, rendering each safe render-only correction that GPT-5.5 requests
- removes ordinary MP4 container metadata and chapters from the final output as a privacy cleanup step
- saves the autonomous decision plan in `input/director_plan.json`

## Viral Captions

The render burns in **word-by-word animated captions** ("karaoke" style) sourced from each scene's
spoken line, synced across the scene window. The currently spoken word is highlighted in a punchy
accent color with a subtle pop; already-spoken words stay white, upcoming words are slightly dimmed.
Each chunk slides up and fades in, with a heavy stroke + drop shadow so text stays readable over any
footage. This is on by default for every render.

Tunable keys in the project config:

- `animated_captions` (default `true`): master switch for the burned-in captions.
- `caption_max_words` (default `3`): words shown on screen at once.
- `caption_uppercase` (default `true`): all-caps caption styling.
- `caption_center_y` (default `0.72`): vertical center as a fraction of height (the hook scene is
  raised automatically).
- `caption_size`: optional fixed font size; auto-scales from width when unset.

## Pacing & Hook

Every render adds **always-on motion energy**: each scene start snaps in with a quick punch-in
zoom that settles over ~0.34s, and the first scene gets a stronger **hook hold** so the opening
frame stops the scroll. The zoom is additive on top of the normal Ken Burns / clip motion and
stays `>= 1.0`, so `cover` framing never reveals edges.

Tunable keys:

- `dynamic_zoom` (default `true`): master switch for the punch-in/hook motion.
- `cut_punch_amount` (default `0.06`) / `cut_punch_seconds` (default `0.34`): per-cut snap size
  and settle time.
- `hook_hold_seconds` (default `0.6`) / `hook_punch_amount` (default `0.12`): opening hook
  intensity and duration.

## Notes

When speech audio is uploaded, the final MP4 keeps that audio primary. Background music and SFX are mixed quietly underneath. The app does not generate voiceover by itself.
The audio mix uses fixed per-source levels plus a limiter, so background music or SFX should not swell louder at the end when other tracks stop.

The final metadata cleanup strips standard MP4 metadata such as title/comment/software/creation time and chapters. It does not attempt to bypass AI provenance, SynthID, invisible watermarks, or detection systems.
