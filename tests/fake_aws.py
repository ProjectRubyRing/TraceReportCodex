#!/usr/bin/env python3
"""A local AWS CLI double; never connects to AWS or reads credentials."""
import datetime as dt
import json
import os
import re
import sys
from pathlib import Path

args = sys.argv[1:]
if args == ["--version"]:
    print("aws-cli/2.99.0 test-double")
    sys.exit(0)
state_path = Path(os.environ["FAKE_AWS_STATE"])
state = json.loads(state_path.read_text(encoding="utf-8"))
action = args[args.index("logs") + 1]
state.setdefault("calls", []).append({"action": action, "args": args})


def option(key):
    return args[args.index(key) + 1]


def finish(value, status=0):
    state_path.write_text(json.dumps(state), encoding="utf-8")
    print(json.dumps(value))
    sys.exit(status)


if action == "start-query":
    if state.get("scenario") == "denied":
        finish({"error": "AccessDeniedException"}, 254)
    query = option("--query-string")
    lo = int(re.search(r">= (\d+)", query).group(1))
    hi = int(re.search(r"< (\d+)", query).group(1))
    identifier = "query-" + str(len(state.setdefault("queries", {})) + 1)
    state["queries"][identifier] = {"lo": lo, "hi": hi, "limit": int(option("--limit")), "polls": 0}
    assert int(option("--start-time")) * 1000 <= lo
    assert int(option("--end-time")) * 1000 >= hi
    finish({"queryId": identifier})
elif action == "stop-query":
    finish({"success": True})
elif action != "get-query-results":
    finish({"error": "unexpected mock command"}, 2)

request = json.loads(option("--cli-input-json")) if "--cli-input-json" in args else {"queryId": option("--query-id")}
query = state["queries"][request["queryId"]]
query["polls"] += 1
scenario = state.get("scenario", "normal")
if scenario == "failed":
    finish({"status": "Failed", "results": []})
if scenario == "timeout" or (scenario == "poll" and query["polls"] == 1):
    finish({"status": "Running", "results": [[{"field": "@message", "value": "This partial result must be ignored"}]]})

matches = []
for row in state.get("records", []):
    value = dt.datetime.fromisoformat(row["@timestamp"]).replace(tzinfo=dt.timezone.utc)
    delta = value - dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)
    timestamp = (delta.days * 86400 + delta.seconds) * 1000 + delta.microseconds // 1000
    if query["lo"] <= timestamp < query["hi"]:
        matches.append(row)
matches.sort(key=lambda row: row["@timestamp"])
limited = matches[:query["limit"]]
response = {"status": "Complete", "statistics": {"recordsMatched": len(matches), "bytesScanned": 8192}}
if scenario in ("paginate", "repeat-token") and len(limited) > 1:
    halfway = len(limited) // 2
    if "nextToken" not in request:
        selected = limited[:halfway]
        response["nextToken"] = "opaque-token-+/$()[]"
    else:
        selected = limited[halfway:]
        if scenario == "repeat-token":
            response["nextToken"] = request["nextToken"]
else:
    selected = limited
response["results"] = [[{"field": key, "value": value} for key, value in row.items()] for row in selected]
finish(response)
