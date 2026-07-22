import subprocess
import json
import re
import sys

def get_scene_changes(video_path):
    cmd = [
        'ffprobe', 
        '-show_frames', 
        '-of', 'json', 
        '-f', 'lavfi', 
        f'movie={video_path},select=gt(scene\,0.4)'
    ]
    # On windows \, might fail in subprocess, let's use a simpler filter
    cmd = [
        'ffprobe',
        '-show_frames',
        '-of', 'json',
        '-f', 'lavfi',
        f'movie={video_path},select=gt(scene\\,0.3)'
    ]
    # Actually just run standard ffprobe
    res = subprocess.run(cmd, capture_output=True, text=True)
    return res.stdout

if __name__ == '__main__':
    print(get_scene_changes("reference.mp4"))
