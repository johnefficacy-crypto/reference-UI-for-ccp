import json
d = json.load(open(r"workbench\nabard_blocks.json", encoding="utf-8"))
for b in d["blocks"]:
    print(b["key"], len(b["questions"]))
