"""Small stdio MCP server used by the proxy tests."""
import json
import os
import sys
from pathlib import Path

calls = Path(sys.argv[1])
tools = [
    {"name": "refund", "inputSchema": {
        "type": "object", "properties": {"amount": {"type": "integer"}},
        "required": ["amount"],
    }},
    {"name": "hidden", "inputSchema": {"type": "object"}},
    {"name": "bulk", "inputSchema": {
        "type": "object", "properties": {"items": {"type": "array"}},
    }},
]


def send(**message):
    print(json.dumps({"jsonrpc": "2.0", **message}), flush=True)


for line in sys.stdin:
    request = json.loads(line)
    method = request.get("method")
    params = request.get("params", {})
    if method is None and request.get("id") == "roots":
        send(method="notifications/roots_received", params=request["result"])
    if method == "notifications/initialized" and "--roots" in sys.argv:
        send(method="roots/list", id="roots")
    if method is None or "id" not in request:
        continue
    if method == "initialize":
        result = {"protocolVersion": params.get("protocolVersion"), "capabilities": {"tools": {}}}
    elif method == "tools/list":
        if "--paged" not in sys.argv:
            result = {"tools": tools}
        elif params.get("cursor") == "page2":
            result = {"tools": tools[1:]}
        else:
            result = {"tools": tools[:1], "nextCursor": "page2"}
    elif method == "tools/call":
        with calls.open("a", encoding="utf-8") as file:
            file.write(json.dumps(params) + "\n")
        result = {
            "content": [{"type": "text", "text": "refunded"}],
            "x0r_env": sorted(key for key in os.environ if key.startswith("X0R_")),
            "argv": sys.argv[2:],
        }
    else:
        result = {"method": method}
    send(id=request["id"], result=result)
