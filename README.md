# ShortsLab

ShortsLab is a local production workspace for creating vertical short-form video from a script,
voiceover, or existing footage. It combines planning, media sourcing, speech timing, editing,
captions, audio finishing, and review in one project folder.

## Run locally

Use `start.bat` to open the desktop-style app window, or `run_app.bat` and visit
`http://127.0.0.1:7865`.

## Core workflows

- **Clip Short** plans a voice-led edit, searches approved social sources, reviews candidate
  footage, cuts to the voiceover, and exports a vertical short.
- **AI Video Station** builds character-led generated-video projects after the voiceover has been
  approved.
- **Effects Station** adds captions, sound effects, visual callouts, or framing adjustments to
  an existing video.
- **Sketch Station** and **Physics Bench** create illustrated explainers and simulation-based
  video.

## Media and review

Projects are stored under `projects/<project_slug>/`. Source media, intermediate reviews, and
renders remain within the project so a cut can be inspected or revised later.

Clip Short reviews exact candidate windows before using them. It writes the selected source,
trim range, evidence, and any replacement flags into the project review files. A budget stop does
not discard completed work: the available edit continues, and any relaxed fallback is marked in
the timeline for review.

## Models and credentials

The selected reasoning model is used throughout a run. The default is Gemini Flash 3.7; the app
does not silently replace it with a premium planning or review model. Configure credentials in
`.env` or the environment, for example:

```powershell
$env:WAVESPEED_API_KEY="..."
```

Model availability and pricing are controlled by the configured provider. Generated images,
video, voice, and model calls may incur provider charges.

## Development

Run the focused test suite with:

```powershell
$env:SHORTSLAB_NO_PAID_API="1"
python -m pytest tests -q
```

The no-paid-API flag prevents tests from sending provider requests.
