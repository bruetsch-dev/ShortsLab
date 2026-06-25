import re

with open("agent_core.py", "r", encoding="utf-8") as f:
    content = f.read()

funcs = [
    "review_and_correct_web_images",
]

for func in funcs:
    content = re.sub(rf"({func}\(.*?)reasoning_model=reasoning_model, (.*?)(\bstatus_cb=)", r"\1\2\3", content, flags=re.DOTALL)
    content = re.sub(rf"({func}\(.*?)(\bstatus_cb=)", r"\1reasoning_model=reasoning_model, \2", content, flags=re.DOTALL)

with open("agent_core.py", "w", encoding="utf-8") as f:
    f.write(content)

print("Calls fixed!")
