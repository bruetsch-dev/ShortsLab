import json
import logging

def fallback_visual_sections(micro_beats):
    """
    Creates a single visual section spanning the whole video if none provided.
    Or groups micro_beats into roughly 5-10s sections.
    """
    if not micro_beats:
        return []
    
    sections = []
    current_section = None
    section_index = 1
    
    for beat in micro_beats:
        start = float(beat.get("start") or beat.get("start_time") or 0.0)
        end = float(beat.get("end") or beat.get("end_time") or 0.0)
        
        if current_section is None:
            current_section = {
                "id": f"section_{section_index:02d}",
                "start": start,
                "end": end,
                "theme": beat.get("visual_meaning", "Introduction"),
                "beats": [beat]
            }
        elif (end - current_section["start"] > 8.0) and len(current_section["beats"]) >= 2:
            # Finish section
            sections.append(current_section)
            section_index += 1
            current_section = {
                "id": f"section_{section_index:02d}",
                "start": start,
                "end": end,
                "theme": beat.get("visual_meaning", "Continuation"),
                "beats": [beat]
            }
        else:
            current_section["end"] = max(current_section["end"], end)
            current_section["beats"].append(beat)
            
    if current_section:
        sections.append(current_section)
        
    return sections

def convert_config_to_edit_plan(config):
    """
    Converts legacy project config containing micro-beats into a strict edit_plan.json schema.
    """
    scenes = config.get("scenes", [])
    
    # Try to extract visual_sections, or fallback
    visual_sections = config.get("visual_sections", [])
    if not visual_sections:
        visual_sections = fallback_visual_sections(scenes)
    
    edit_plan = {
        "version": "2.0",
        "project_slug": config.get("project_slug", ""),
        "title": config.get("title", ""),
        "duration": config.get("duration", 0.0),
        "visual_sections": [],
        "audio_path": config.get("audio_path"),
        "seedance_audio_segments": config.get("seedance_audio_segments", []),
        "sfx_segments": config.get("sfx_segments", []),
        "background_music": config.get("background_music"),
        "render_options": {
            "resolution": config.get("resolution", [1080, 1920]),
            "fps": config.get("fps", 30)
        }
    }
    
    # Map scenes back into their sections if necessary, or just use the sections
    for sec in visual_sections:
        # A section in the new format has beats
        beats = sec.get("beats", [])
        if not beats:
            # Reconstruct beats for this section based on time
            start = float(sec.get("start", 0))
            end = float(sec.get("end", 999))
            beats = [b for b in scenes if float(b.get("start", 0)) >= start and float(b.get("end", 0)) <= end]
            
        mapped_beats = []
        for b in beats:
            mapped_beats.append({
                "id": b.get("id"),
                "start": b.get("start"),
                "end": b.get("end"),
                "exact_voice_text": b.get("exact_voice_text", ""),
                "visual_meaning": b.get("visual_meaning", ""),
                "viewer_emotion": b.get("viewer_emotion", ""),
                "visual_hook_type": b.get("visual_hook_type", ""),
                "best_media_type": b.get("best_media_type", "web_image"),
                "shot_type": b.get("shot_type", ""),
                "camera_motion": b.get("camera_motion", ""),
                "asset_path": b.get("seedance_path") or b.get("gpt_image_path") or b.get("web_image_path"),
                "prompt": b.get("prompt", ""),
                "video_prompt": b.get("video_prompt", "")
            })
            
        edit_plan["visual_sections"].append({
            "id": sec.get("id", "section"),
            "start": sec.get("start", 0.0),
            "end": sec.get("end", 0.0),
            "theme": sec.get("theme", ""),
            "beats": mapped_beats
        })
        
    return edit_plan
