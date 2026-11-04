# Execution policy, evidence and remaining limits

This is an experimental backend, not an end-user authorization platform or a certified security product.

## Implemented boundary

Before each built-in tool runs, the execution service checks:

1. The agent belongs to the current workspace.
2. The tool is granted in the agent configuration.
3. The tool is permitted by the operator-controlled `X0R_OPERATOR_TOOL_ALLOWLIST`.
4. The tool name is recognized and its argument shape and input length are accepted.
5. HTTP destinations satisfy an additional host allowlist, URL credential and port check.

The default operator list is `knowledge_search,calculator`. HTTP remains unavailable unless an operator explicitly grants `http_get` and configures `X0R_HTTP_TOOL_ALLOWLIST`. Custom registry tools are denied until policy support is explicitly implemented. This is a deliberate compatibility change.

Retrieved text and model-generated tool requests cannot modify operator settings through this execution path. This does not prevent all prompt injection: an agent can still misuse an otherwise permitted operation or generate misleading answers.

Denials stop the run with `status="blocked"` without invoking the requested handler. A `policy` event records version, workspace, tool, allow/deny and reason; it omits argument values. Step or per-step tool-call limits produce `limit_exceeded`, not `succeeded`. Existing consumers must accept these additional status values. Operators can configure `X0R_MAX_TOOL_CALLS_PER_STEP` (default 8).

## Evidence limits

Policy events are ordinary database records, not tamper-evident forensic evidence. Existing model/tool steps and run inputs/outputs still store raw content and may contain secrets or personal information. There is no complete redaction, retention or deletion policy yet. Use synthetic data, restrict database access and define retention before any sensitive deployment. A recorded sequence is not proof of causation.

## Resource and authorization limits

- Monthly usage checks are admission checks based on recorded usage, not atomic spend reservations. Concurrent runs and a single large run can overshoot a quota. There is no hard cost guarantee, and embedding usage is tracked separately.
- HTTP validates DNS before HTTPX makes a separate connection. DNS rebinding/TOCTOU is not resolved. Responses are not streamed with a byte cap. Keep HTTP disabled for sensitive deployments until these are fixed with connection-bound destination validation and network egress controls.
- Uploaded files are read at most `max_upload_bytes + 1` into the endpoint's buffer. Multipart parsing and proxy ingress still need independent request-size limits; JSON document content also needs an explicit size limit.
- Operator permissions apply globally and agent permissions apply within a workspace. API keys do not have per-user roles or resource-level permissions. Workspace-scoped access alone is not end-user RBAC.
- The calculator restricts AST operations and input length but is not an isolated CPU/memory sandbox.
- Mock/scripted-provider tests establish execution-control behavior, not resistance of a real model to arbitrary attacks. No provider API calls are needed for this test suite.

## Next independently testable work

1. Atomic budget reservation and reconciliation, including failure paths and concurrent runs.
2. Connection-bound HTTP destination validation and streaming byte/time limits.
3. Redaction, evidence access controls, retention and deletion.
4. Caller/resource permissions, then real-model adversarial tests with explicit cost limits.

## Local verification

`python -m pytest -q` exercises the original tests plus policy allow/deny, argument validation, workspace mismatch, blocked-handler behavior and limit outcomes. No production systems or real credentials are involved.
