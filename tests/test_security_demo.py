"""Check the installed CLI boundary, including isolation from caller configuration."""
from __future__ import annotations

import json
import os
import subprocess
import sys


def test_demo_is_repeatable_and_ignores_external_configuration(tmp_path):
    (tmp_path / ".env").write_text("X0R_PROVIDER=invalid\nX0R_MAX_TOOL_CALLS_PER_STEP=0\n")
    env = dict(os.environ, X0R_PROVIDER="invalid", X0R_DATABASE_URL="invalid://unused",
               X0R_OPERATOR_TOOL_ALLOWLIST="http_get", X0R_MAX_TOOL_CALLS_PER_STEP="0")
    outputs = []
    for _ in range(2):
        result = subprocess.run(
            [sys.executable, "-I", "-m", "x0r_core.cli", "security-demo"],
            cwd=tmp_path, env=env, capture_output=True, text=True, check=True, timeout=30,
        )
        report = json.loads(result.stdout)
        assert report["checks"] == "passed"
        assert report["provider"] == "mock"
        denied, permitted = report["cases"]
        assert (denied["status"], denied["handler_calls"], denied["tool_steps"]) == (
            "blocked", 0, 0
        )
        assert denied["policy"][0]["reason"] == "not_granted_by_operator"
        assert (permitted["status"], permitted["handler_calls"], permitted["tool_steps"]) == (
            "succeeded", 1, 1
        )
        assert permitted["answer_contains_24_months"] is True
        outputs.append(result.stdout)
    assert outputs[0] == outputs[1]
    assert sorted(p.name for p in tmp_path.iterdir()) == [".env"]
