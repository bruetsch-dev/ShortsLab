"""Verify the exported asset and its delivery through the real static handler."""
import json
import struct
import unittest
from pathlib import Path
from tests.test_shell_v3_wiring import get

ROOT = Path(__file__).resolve().parents[1]


class BlenderShowroom(unittest.TestCase):
    def test_each_station_has_its_own_computer_desk_and_animated_props(self):
        data = (ROOT / 'static/showroom3d/shortslab-stations.glb').read_bytes()
        magic, version, length = struct.unpack_from('<4sII', data)
        self.assertEqual((magic, version, length), (b'glTF', 2, len(data)))
        chunk_length = struct.unpack_from('<I', data, 12)[0]
        model = json.loads(data[20:20 + chunk_length])
        nodes = model['nodes']
        def descendants(node):
            yield node
            for index in node.get('children', []):
                yield from descendants(nodes[index])
        roots = {n['name']: n for n in nodes if n.get('name', '').startswith('STATION_')}
        self.assertEqual(set(roots), {'STATION_' + mode for mode in ('clip', 'ai', 'longform', 'physics', 'enhance')})
        for name, root in roots.items():
            with self.subTest(station=name):
                parts = [nodes[i]['name'] for i in root['children']]
                for prefix in ('PART_desk', 'PART_computer', 'PART_keyboard'):
                    self.assertEqual(sum(p.startswith(prefix) for p in parts), 1)
                self.assertGreaterEqual(len(parts), 5)
                screens = [n for n in descendants(root) if n.get('name', '').startswith('SCREEN_DISPLAY')]
                self.assertEqual(len(screens), 1)
                primitive = model['meshes'][screens[0]['mesh']]['primitives'][0]
                self.assertIn('TEXCOORD_0', primitive['attributes'])

    def test_runtime_model_is_valid_and_contains_the_interactive_screen(self):
        data = (ROOT / 'static/showroom3d/shortslab-retro.glb').read_bytes()
        magic, version, length = struct.unpack_from('<4sII', data)
        self.assertEqual((magic, version, length), (b'glTF', 2, len(data)))
        chunk_length, kind = struct.unpack_from('<I4s', data, 12)
        self.assertEqual(kind, b'JSON')
        model = json.loads(data[20:20 + chunk_length])
        screens = [node for node in model['nodes'] if node.get('name') == 'SCREEN_DISPLAY']
        self.assertEqual(len(screens), 1)
        primitive = model['meshes'][screens[0]['mesh']]['primitives'][0]
        self.assertIn('TEXCOORD_0', primitive['attributes'])
        self.assertFalse(model.get('images'), 'The physical set must remain self-contained.')

    def test_glb_and_local_imports_are_served_with_correct_mime_types(self):
        for name, mime in [('shortslab-retro.glb', 'model/gltf-binary'),
                           ('shortslab-stations.glb', 'model/gltf-binary'),
                           ('scene.js', 'javascript'), ('cinematic.js', 'javascript'), ('crt-display.js', 'javascript'), ('showroom.css', 'text/css'),
                           ('vendor/three.module.js', 'javascript'),
                           ('vendor/three.core.js', 'javascript'),
                           ('vendor/GLTFLoader.js', 'javascript'),
                           ('vendor/BufferGeometryUtils.js', 'javascript')]:
            with self.subTest(name=name):
                result = get('/static/showroom3d/' + name)
                self.assertIsNone(result.error)
                self.assertIn(mime, result.content_type)
                self.assertGreater(len(result.body), 100)

    def test_asset_route_does_not_expose_source_or_parent_files(self):
        for name in ['shortslab-retro.blend', '../shell-v3.js', '../../app.py']:
            with self.subTest(name=name):
                self.assertEqual(get('/static/showroom3d/' + name).error, 404)


if __name__ == '__main__':
    unittest.main()
