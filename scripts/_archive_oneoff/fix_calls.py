import re

with open("agent_core.py", "r", encoding="utf-8") as f:
    content = f.read()

funcs = [
    "llm_generate_project_title",
    "llm_micro_beat_plan",
    "llm_search_plan",
    "llm_auto_director_plan",
    "llm_speaker_clip_plan",
    "llm_web_image_review",
    "llm_prompt_review",
    "llm_video_review",
    "llm_pre_render_edit_review",
]

for func in funcs:
    content = re.sub(rf"({func}\(.*?)reasoning_model=reasoning_model, (.*?)(\bstatus_cb=)", r"\1\2\3", content, flags=re.DOTALL)
    content = re.sub(rf"({func}\(.*?)(\bstatus_cb=)", r"\1reasoning_model=reasoning_model, \2", content, flags=re.DOTALL)

with open("agent_core.py", "w", encoding="utf-8") as f:
    f.write(content)

print("Calls fixed!")
