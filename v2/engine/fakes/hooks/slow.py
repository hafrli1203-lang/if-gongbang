import json, sys, time
json.load(sys.stdin)
time.sleep(5)
print(json.dumps({"decision": "allow", "reason": "late"}))
