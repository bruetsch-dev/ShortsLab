"""Encode only a complete Blender proof and validate the resulting video."""
import json
import struct
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'static/showroom3d'
BIN = ROOT / 'tools/runtime/ffmpeg-9.0.1/ffmpeg-9.0.1-full_build-shared/bin'
frames = OUT / 'motion-preview-frames'
for frame in range(1, 241):
    path = frames / f'{frame:04d}.png'
    with path.open('rb') as stream:
        header = stream.read(24)
    if header[:8] != b'\x89PNG\r\n\x1a\n' or struct.unpack('>II', header[16:24]) != (960, 540):
        raise ValueError(f'Invalid preview frame: {path.name}')

movie = OUT / 'shortslab-motion-preview.mp4'
subprocess.run([str(BIN/'ffmpeg.exe'), '-hide_banner', '-loglevel', 'error', '-y',
                '-framerate', '24', '-start_number', '1', '-i', str(frames/'%04d.png'),
                '-frames:v', '240', '-c:v', 'libx264', '-preset', 'slow', '-crf', '17',
                '-pix_fmt', 'yuv420p', '-movflags', '+faststart', str(movie)], check=True)
probe = subprocess.run([str(BIN/'ffprobe.exe'), '-v', 'error', '-select_streams', 'v:0',
                       '-show_entries', 'stream=width,height,nb_frames,r_frame_rate,duration',
                       '-of', 'json', str(movie)], check=True, capture_output=True, text=True)
stream = json.loads(probe.stdout)['streams'][0]
assert (stream['width'], stream['height'], stream['nb_frames'], stream['r_frame_rate']) == (960,540,'240','24/1'),stream
assert abs(float(stream['duration'])-10)<.01,stream
print('MOTION_VIDEO_VERIFIED', movie, json.dumps(stream), flush=True)
