"""An arrow has to point at the picture, not at its own tile on the contact sheet.

The visual-FX planner hands the model ONE contact sheet with a frame per scene and asks for
coordinates "0-1 WITHIN each tile". It answers about the image it was given - the whole sheet.
Measured on the school-rules Short (2026-09-05), the twelve arrows came back as:

    (0.18,0.16) (0.45,0.11) (0.86,0.12) (0.15,0.44) (0.48,0.38) (0.87,0.39)
    (0.18,0.65) (0.51,0.61) (0.84,0.61) (0.16,0.85) (0.43,0.87) (0.82,0.85)

which is the centre of each scene's own tile in a 3-column, 4-row grid. Every arrow pointed at
itself.
"""
import json

import agent_core

LAYOUT = {"cols": 3, "rows": 4, "cell_w": 260, "cell_h": 420, "header_h": 54,
          "sheet_w": 780, "sheet_h": 1734, "count": 12}
MEASURED = [(0.18, 0.16), (0.45, 0.11), (0.86, 0.12), (0.15, 0.44), (0.48, 0.38), (0.87, 0.39),
            (0.18, 0.65), (0.51, 0.61), (0.84, 0.61), (0.16, 0.85), (0.43, 0.87), (0.82, 0.85)]


def test_every_measured_point_belongs_to_its_own_tile():
    for index, (cx, cy) in enumerate(MEASURED):
        assert agent_core.sheet_point_to_tile(cx, cy, index, LAYOUT) is not None, index


def test_conversion_lands_inside_the_frame():
    for index, (cx, cy) in enumerate(MEASURED):
        x, y = agent_core.sheet_point_to_tile(cx, cy, index, LAYOUT)
        assert 0.0 <= x <= 1.0 and 0.0 <= y <= 1.0


def test_a_point_in_another_tile_is_refused():
    """Scene 0 sits top-left; a point in the bottom-right tile is not its target."""
    assert agent_core.sheet_point_to_tile(0.82, 0.85, 0, LAYOUT) is None


def test_a_genuine_tile_coordinate_is_left_alone():
    """When the model really does answer in tile space, the point is outside the tile's own
    slice of the sheet and must not be converted twice."""
    assert agent_core.sheet_point_to_tile(0.72, 0.30, 0, LAYOUT) is None


def test_the_top_left_tile_maps_to_the_top_left_of_the_frame():
    x, y = agent_core.sheet_point_to_tile(0.02, 0.04, 0, LAYOUT)
    assert x < 0.1 and y < 0.1


def test_a_broken_layout_is_survivable():
    assert agent_core.sheet_point_to_tile(0.5, 0.5, 0, {}) is None
    assert agent_core.sheet_point_to_tile(0.5, 0.5, 0, None) is None


def test_the_sheet_writes_its_layout(tmp_path):
    from PIL import Image
    paths = []
    for i in range(5):
        p = tmp_path / f"{i}.jpg"
        Image.new("RGB", (200, 320), (i * 40, 80, 120)).save(p)
        paths.append(p)
    out = agent_core.create_media_contact_sheet(paths, tmp_path / "sheet.jpg")
    layout = json.loads(out.with_suffix(".layout.json").read_text(encoding="utf-8"))
    assert layout["cols"] == 3 and layout["rows"] == 2 and layout["count"] == 5
    assert layout["sheet_w"] == layout["cols"] * layout["cell_w"]


def test_the_planner_treats_a_tile_centre_as_no_target():
    import inspect
    body = inspect.getsource(agent_core.plan_visual_fx)
    assert "sheet_point_to_tile(cx, cy" in body
    assert 'd = dict(d, confidence=0.0, callout="none")' in body
