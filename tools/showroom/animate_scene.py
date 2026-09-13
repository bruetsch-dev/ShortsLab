"""Editable 10-second camera sequence from the physical showroom source.

Blender --background --python tools/showroom/animate_scene.py -- --proofs
"""
import bpy
import math
import sys
from pathlib import Path
from mathutils import Vector

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'static/showroom3d'
bpy.ops.wm.open_mainfile(filepath=str(OUT / 'shortslab-retro.blend'))
scene = bpy.context.scene
camera = scene.camera
camera.animation_data_clear()
camera.data.animation_data_clear()
scene.render.fps = 24
scene.frame_start, scene.frame_end = 1, 240
scene.render.resolution_x, scene.render.resolution_y = 1920, 1080
scene.render.resolution_percentage = 100
camera.data.clip_start = .015
camera.data.dof.use_dof = True
camera.data.dof.aperture_fstop = 5.6

focus = bpy.data.objects.new('FOCUS | CRT glass', None)
scene.collection.objects.link(focus)
focus.location = (0, -.23, 1.872)
camera.data.dof.focus_object = focus

def ease(value):
    x = max(0, min(1, value))
    return x*x*x*(x*(x*6-15)+10)

home = Vector((3.0, -7.2, 2.9))
aim = Vector((-.85, 0, 1.36))
approach = Vector((2.65, -7.17, 2.9))
glass = Vector((0, -.18, 1.872))
end = Vector((0, -.31, 1.872))
control1 = Vector((approach.x*.65, approach.y*.55, 2.16))
control2 = Vector((0, -1.15, 1.872))

for frame in range(1, 241):
    if frame <= 78:
        k = ease((frame-1)/77)
        position = home + Vector((-.65, -.95, .16))*(1-k)
        target = aim
        lens = 41 + 3*k
    elif frame <= 168:
        k = ease((frame-78)/90)
        position = home.lerp(approach, k)
        target = aim
        lens = 44
    else:
        k = ease((frame-168)/72)
        a = 1-k
        position = approach*a**3 + control1*(3*a*a*k) + control2*(3*a*k*k) + end*k**3
        target = aim.lerp(glass, k)
        lens = 44
    camera.location = position
    camera.rotation_euler = (target-position).to_track_quat('-Z', 'Y').to_euler()
    camera.data.lens = lens
    camera.keyframe_insert(data_path='location', frame=frame)
    camera.keyframe_insert(data_path='rotation_euler', frame=frame)
    camera.data.keyframe_insert(data_path='lens', frame=frame)

for frame, name in [(1,'01 / SLOW REVEAL'),(78,'02 / WORKSTATION'),(168,'03 / ENTER CRT'),(240,'04 / SCREEN')]:
    marker=scene.timeline_markers.new(name,frame=frame)
scene.cycles.samples = 64
scene.cycles.use_denoising = True
scene.render.image_settings.file_format = 'PNG'
scene.render.filepath = str(OUT / 'cinematic-frames' / 'frame_')
scene.frame_set(78)
bpy.ops.wm.save_as_mainfile(filepath=str(OUT / 'shortslab-cinematic.blend'))
print('CINEMATIC_READY: 240 frames / 24fps / 1920x1080 / animated lens and CRT focus', flush=True)

if '--proofs' in sys.argv:
    scene.render.resolution_percentage = 50
    scene.cycles.samples = 16
    for frame in (1, 120, 212):
        scene.frame_set(frame)
        scene.render.filepath = str(OUT / f'cinematic-proof-{frame:03d}.png')
        bpy.ops.render.render(write_still=True)
