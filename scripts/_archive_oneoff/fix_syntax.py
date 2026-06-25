import re
with open("agent_core.py", "r", encoding="utf-8") as f:
    content = f.read()
content = content.replace("reasoning_model=None, reasoning_model=reasoning_model, ", "reasoning_model=None, ")
content = content.replace("reasoning_model=None, reasoning_model=reasoning_model, ", "reasoning_model=None, ") # Double run
with open("agent_core.py", "w", encoding="utf-8") as f:
    f.write(content)
print("Syntax fixed")
