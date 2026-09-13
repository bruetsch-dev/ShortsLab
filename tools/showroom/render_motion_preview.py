"""Render a resumable motion proof; keep full-quality source settings intact."""
import bpy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'static/showroom3d'
bpy.ops.wm.open_mainfile(filepath=str(OUT / 'shortslab-cinematic.blend'))
scene = bpy.context.scene
scene.render.resolution_percentage = 50
scene.cycles.samples = 24
scene.cycles.use_denoising = True
scene.render.image_settings.file_format = 'PNG'
frames = OUT / 'motion-preview-frames'
frames.mkdir(exist_ok=True)
for frame in range(scene.frame_start, scene.frame_end + 1):
    path = frames / f'{frame:04d}.png'
    if path.exists():
        continue
    scene.frame_set(frame)
    scene.render.filepath = str(path)
    bpy.ops.render.render(write_still=True)
    print(f'MOTION_FRAME {frame}/{scene.frame_end}', flush=True)
print('MOTION_FRAMES_COMPLETE', flush=True)
