"""Fetch the pinned MIT-licensed Three.js runtime for local/offline serving."""
import urllib.request
from pathlib import Path

root = Path(__file__).resolve().parents[2] / 'static/showroom3d/vendor'
root.mkdir(parents=True, exist_ok=True)
files = {'three.module.js': 'build/three.module.js', 'three.core.js': 'build/three.core.js',
         'GLTFLoader.js': 'examples/jsm/loaders/GLTFLoader.js',
         'BufferGeometryUtils.js': 'examples/jsm/utils/BufferGeometryUtils.js', 'LICENSE': 'LICENSE'}
for name, src in files.items():
    data = urllib.request.urlopen('https://cdn.jsdelivr.net/npm/three@0.180.0/' + src, timeout=35).read()
    if name in ('GLTFLoader.js', 'BufferGeometryUtils.js'):
        data = data.replace(b"'three'", b"'./three.module.js'").replace(
            b"'../utils/BufferGeometryUtils.js'", b"'./BufferGeometryUtils.js'")
    (root / name).write_bytes(data)
    print(name, len(data))
