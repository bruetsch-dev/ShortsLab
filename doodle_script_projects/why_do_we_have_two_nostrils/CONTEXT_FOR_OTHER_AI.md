# Context for continuing this AutoShortsClaude task

## User and communication

- The user speaks German, but the actual video assets (title, voice-over, and prompts) are in English.
- The user wants curiosity-driven explainer topics in the style of “The First Dog” and “How Ancient Humans Slept”: everyday questions with surprising evolutionary, biological, or historical explanations.
- Always offer five topic choices plus a sixth option for a new/custom topic before starting a new project.
- Workflow order is strict: choose topic -> write the full script (about 3,000 tokens) -> only then create image prompts.
- Do not generate voice-over unless the user explicitly asks. The user previously said there were no voice-over credits and wanted to start only image generation.

## Workspace and project

- Workspace: `D:\data\AutoShortsClaude`
- General project area: [doodle_script_projects](D:\data\AutoShortsClaude\doodle_script_projects)
- Current project: [why_do_we_have_two_nostrils](D:\data\AutoShortsClaude\doodle_script_projects\why_do_we_have_two_nostrils)
- Topic/title: **Why Do We Have Two Nostrils?**

## Final script

- [script.txt](D:\data\AutoShortsClaude\doodle_script_projects\why_do_we_have_two_nostrils\script.txt)
- Length: 3,342 words (about 3,000 tokens plus the requested extra 1,000 words).
- Estimated narration length: about 20–22 minutes depending on delivery speed.
- The script explains the nasal cycle, turbinate airflow, warming/humidifying/filtering, odor sampling, stereo olfaction, scent tracking, bilateral anatomy, and why the exact evolutionary purpose is not one single proven explanation.
- Sources and accuracy notes: [research_sources.md](D:\data\AutoShortsClaude\doodle_script_projects\why_do_we_have_two_nostrils\research_sources.md)

## Final image prompts

- [image_prompts.txt](D:\data\AutoShortsClaude\doodle_script_projects\why_do_we_have_two_nostrils\image_prompts.txt)
- [image_prompts_comfyui.txt](D:\data\AutoShortsClaude\doodle_script_projects\why_do_we_have_two_nostrils\image_prompts_comfyui.txt)
- [scene_descriptions.tsv](D:\data\AutoShortsClaude\doodle_script_projects\why_do_we_have_two_nostrils\scene_descriptions.tsv)
- There are 170 timestamped scenes from 0:00 to 22:12, with no blank lines.
- Every generated prompt contains the consistent style: hand-drawn 2D doodle cartoon animation, flat colors, bold black outlines, slightly imperfect sketchy marker lines, no gradients, no shadows, no textures, no photorealism, no 3D, 16:9, educational YouTube explainer doodle style.
- `image_prompts.txt` keeps timestamps such as `[0:00]` for the AutoShortsClaude Higgsfield uploader.
- `image_prompts_comfyui.txt` removes timestamps and is one prompt per line for ComfyUI.
- Prompt export/build script: [build_prompts.ps1](D:\data\AutoShortsClaude\doodle_script_projects\why_do_we_have_two_nostrils\build_prompts.ps1). It writes UTF-8 without BOM so the first timestamp parses correctly.

## Higgsfield generation result

- Higgsfield was connected through the saved local profile at `D:\data\AutoShortsClaude\higgsfield-profile`.
- The app was run in the dedicated Longform Image Set mode, FLUX.2 Pro, 16:9.
- The output folder is [projects\_longform\image_prompts](D:\data\AutoShortsClaude\projects\_longform\image_prompts).
- Verified final files: **170/170 PNG frames**, plus `thumbnail_why_two_nostrils.png` and `state.json`.
- Verified first and last frame exist: `[0-00].png` and `[22-12].png`.
- The in-memory job ID may no longer be available after restarting the local app, but the rendered files are complete and are the authoritative result.

## App changes made during generation

- `app.py` was patched so uploaded prompt files use the manual Higgsfield Unlimited session and the existing pool rather than opening a fresh page for every image.
- The pool input bug was fixed: `generate_pool_sync` expects tuples `(idx, prompt, path)`, not dictionaries.
- The first trial job used the old automatic path and was cancelled with 0 images; no successful image render was lost in that trial.
- The successful run required Unlimited to be enabled once in the visible Higgsfield window. No voice-over call was made.
- Local app URL when running: `http://127.0.0.1:7865/`

## Important continuation guidance

1. Do not regenerate the 170 frames unless the user requests variants or a correction.
2. First inspect a contact sheet or selected frames if the user asks for visual quality review.
3. If a frame needs regeneration, preserve its timestamp filename and use the matching prompt line only.
4. Do not run the full longform video pipeline: it would attempt voice-over/TTS and is outside the current request.
5. If creating another topic, make a new slugged subfolder under `doodle_script_projects`, keep the same script-then-prompts order, and offer five choices plus “new topic” first.
