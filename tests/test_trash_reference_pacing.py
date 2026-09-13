import agent_core


def test_toilet_shaped_timeline_coalesces_to_trash_reference_count():
    """The shipped Toilet edit had 18 placements; the Trash reference has 12 in ~31 seconds."""
    durations = [
        2.01, 1.73, 1.41, 1.41, 2.18, 2.82, 1.79, 1.79, 2.30,
        1.31, 1.33, 2.14, 1.79, 1.60, 1.95, 2.08, 1.41, 2.143,
    ]
    scenes = []
    start = 0.0
    for index, duration in enumerate(durations):
        scenes.append({
            "id": f"beat_{index}", "start": start, "end": start + duration,
            "script": f"continuous visual thought {index}",
        })
        start += duration
    paced = agent_core.coalesce_scrape_visual_chapters(
        scenes, min_s=1.55, target_s=2.55, max_s=3.6)
    paced = agent_core.enforce_fact_short_max_hold(paced, max_s=3.6)
    assert len(paced) == 12
    assert max(scene["end"] - scene["start"] for scene in paced) <= 3.6
