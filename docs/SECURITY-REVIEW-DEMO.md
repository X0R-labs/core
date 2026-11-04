# Offline security-review demonstration

**Deterministic mock testing of our own experimental X0R Core.** This is not a client engagement, independent audit, real-model attack evaluation or proof of production security. All records are synthetic.

## Run it

Requires Python 3.11 or 3.12. From a checkout of Core:

```bash
python3 -m venv /tmp/x0r-demo-venv
/tmp/x0r-demo-venv/bin/python -m pip install .
/tmp/x0r-demo-venv/bin/x0r security-demo
```

Use a fresh temporary path if that directory already exists. Installing dependencies may need network access; the installed command runs offline without API keys, a server, bootstrap or paid model calls. It launches a fresh Python child, removes inherited `X0R_` settings, changes to a temporary directory without a `.env`, explicitly selects `MockProvider`, uses in-memory SQLite and disables embeddings. No local application database or API keys are read or created by the worker; temporary records are discarded on exit.

The process still inherits the normal OS environment and installed dependencies. This is demo configuration/data isolation, not an OS sandbox. Do not invoke the worker inside an API server. Only in this disposable worker, a wrapper counts entries to the registered handler and delegates permitted calls to the real implementation.

## Expected evidence

The output is stable JSON, excluding generated record IDs, latency and raw transcripts. See [complete expected output](security-demo.expected.json).

| Case | Agent grants | Operator grants | Status | Policy reason | Handler entries | Tool steps |
| --- | --- | --- | --- | --- | --- | --- |
| operator_denied | knowledge_search | calculator | blocked | not_granted_by_operator | 0 | 0 |
| permitted | knowledge_search | knowledge_search,calculator | succeeded | granted | 1 | 1 |

Both cases ask “How long is the synthetic warranty?” The only document states a 24-month warranty. The same mock requests `knowledge_search` in both cases. Only the operator configuration changes; the prompt and agent grants do not. The permitted answer contains `24 months`; the blocked answer does not.

`checks: passed` is printed only after checking status, policy, handler entries, tool steps and answer content. Mismatches raise an error and exit nonzero even with Python optimization. A direct entry counter matters: a missing tool trace alone would not prove the handler never ran. Policy summaries contain version, tool, allow/deny and reason. Actual policy records also contain the workspace ID, omitted from this summary for reproducibility. Policy events omit arguments; other run steps still retain raw content.

## Regression evidence

```bash
python -m pytest -q tests/test_execution_policy.py tests/test_security_demo.py
ruff check src tests
```

The existing `test_denied_tool_never_executes` now covers both a custom tool and the built-in lookup using the mock provider. `test_permitted_mock_tool_executes_once` delegates to actual retrieval while counting entries. The CLI regression repeats the command with hostile `.env` and inherited settings, checks identical output and verifies no application files appear in the caller directory. Existing API success-path, workspace, argument and execution-limit tests remain in place.

Read the [sample review](SAMPLE-SECURITY-REVIEW.md) and [security model](SECURITY-MODEL.md). Organization pinning, the rendered authenticated issue picker and physical iPhone Safari remain pending user checks, separate from this demonstration.
