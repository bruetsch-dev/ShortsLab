# Shortslab Blender showroom

The editable source is `static/showroom3d/shortslab-retro.blend`.
The browser loads `shortslab-retro.glb`; it contains the desk, computer, keyboard,
archive tapes, telephone, lamp and room as real geometry. The carousel and existing
editing screens remain in `static/shell-v3.js`.

## Rebuild

```powershell
& 'D:\data\Blender\blender.exe' --background --python tools/showroom/build_scene.py -- --render
```

Omit `-- --render` to export without the Cycles reference image. The deterministic
builder regenerates both the editable scene and the browser asset. Manual edits to
the `.blend` file should be exported directly from Blender instead of running the
builder again.

Keep the UV-mapped mesh named `SCREEN_DISPLAY`: the browser replaces its material
with the current station's live CRT display. Blender uses Z up; glTF exports Y up.
The camera destination and screen interaction bounds are in `scene.js`.

The browser provides lighting, shadows, the video wall, subtle camera movement and
the camera flight into the selected tool. Reduced-motion mode uses a still poster
and immediate entry. Shadow maps are computed once because the furniture is static;
pixel ratio is capped at 1.5, rendering at up to 60 fps, and resources are disposed when
leaving the showroom.

`cinematic.js` supplies the antialiased HDR lens pass and the curved CRT approach.
The opening move lasts 3.2 seconds; station changes smoothly adjust the viewpoint
and rim-light color. Screen entry follows a 2.1-second curve, clearing CRT text
before the close-up. Reduced-motion mode skips these camera moves.
Scrolling advances the whole workstation vertically: one captured outgoing frame
slides up while the live next station enters from below. The temporary canvas is
removed when the transition finishes or is interrupted. Arrival into a tool uses
the current UI's `--stage` color, including a gradual tint of the CRT display.

## Editable cinematic sequence

`shortslab-cinematic.blend` contains a separate 10-second, 24 fps sequence with
camera and lens keyframes, focus on the CRT glass, and named timeline markers.
It preserves the static modeling source. Rebuild it with:

```powershell
& 'D:\data\Blender\blender.exe' --background --python tools/showroom/animate_scene.py -- --proofs
```

The saved scene is configured for 1920×1080 PNG sequences and 64 Cycles samples.
`--proofs` additionally renders three half-resolution review frames; it does not
render the full movie. The image sequence can subsequently be imported into an
editor. No DaVinci Resolve project or grade has been created.

For a complete 960×540 motion proof, run `render_motion_preview.py` with Blender.
It renders all 240 frames at 24 Cycles samples, resuming missing frames after an
interruption. These intermediate PNGs are ignored by Git. The editable scene's
full-resolution render settings are preserved.

Three.js 0.180.0 and its glTF loader are pinned and served locally. Their MIT license
is in `static/showroom3d/vendor/LICENSE`. `vendor_three.py` reproduces the download
from the official npm package through jsDelivr.

```powershell
python -m unittest tests.test_blender_showroom tests.test_showroom_machine tests.test_shell_v3_wiring tests.test_shell_v3_windows
```
