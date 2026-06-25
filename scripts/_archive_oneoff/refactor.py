import re

def main():
    with open("agent_core.py", "r", encoding="utf-8") as f:
        content = f.read()

    # Rename log mentions of GPT-5.5
    content = content.replace("GPT-5.5", "Reasoning Agent")
    # Rename functions
    content = content.replace("gpt55_", "llm_")
    
    # Now, add reasoning_model to the function signatures
    # We want to replace:
    # def llm_micro_beat_plan(title, script, visual_script, target_duration, base_scenes, status_cb=None):
    # with
    # def llm_micro_beat_plan(title, script, visual_script, target_duration, base_scenes, reasoning_model=None, status_cb=None):
    # Actually, it's easier to just do:
    content = re.sub(
        r'def llm_([a-z_]+)\((.*?)(status_cb=None)\):',
        r'def llm_\1(\2reasoning_model=None, \3):',
        content
    )
    
    # We also need to change "model": GPT55_MODEL, to "model": reasoning_model or GPT55_MODEL,
    content = content.replace('"model": GPT55_MODEL,', '"model": reasoning_model or GPT55_MODEL,')

    with open("agent_core.py", "w", encoding="utf-8") as f:
        f.write(content)

    print("agent_core.py refactored.")

    with open("app.py", "r", encoding="utf-8") as f:
        content2 = f.read()

    content2 = content2.replace("GPT-5.5", "Reasoning Agent")
    content2 = content2.replace("gpt55_", "llm_")
    
    with open("app.py", "w", encoding="utf-8") as f:
        f.write(content2)

    print("app.py refactored.")

if __name__ == "__main__":
    main()
