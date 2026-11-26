from __future__ import annotations

import subprocess
import sys
from pathlib import Path

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"

SCRIPT = """
import sys
sys.path.insert(0, sys.argv[1])
import custom_tool  # imports x0r_core.tools first and registers order_lookup
from x0r_core.config import Settings
from x0r_core.policy import authorize_tool
d = authorize_tool(workspace_id="a", agent_workspace_id="a", agent_tools=["order_lookup"],
                   tool_name="order_lookup", arguments={"order_id": "A-1001"},
                   settings=Settings(operator_tool_allowlist="order_lookup"))
print(d.allowed, d.reason)
"""


def test_custom_tool_example_imports_and_is_authorized():
    # A fresh interpreter, so import order matches a real user script.
    out = subprocess.run(
        [sys.executable, "-c", SCRIPT, str(EXAMPLES)],
        capture_output=True, text=True, check=True,
    )
    assert out.stdout.strip() == "True granted"
