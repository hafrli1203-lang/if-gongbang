import json, sys
json.load(sys.stdin)
print(json.dumps({"decision": "allow", "reason": "ok"}))
