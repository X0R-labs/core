# API

Base URL: `http://localhost:8000`  
Auth: `Authorization: Bearer x0r_...` or `X-API-Key: x0r_...`  
Interactive docs: `/docs` (OpenAPI at `/openapi.json`)

Every response is workspace-scoped by the key. Ids from another workspace return `404`.

## Ops

| Method | Path | Notes |
| --- | --- | --- |
| `GET` | `/health` | Public. Provider and version. |
| `GET` | `/` | Public. Endpoint map. |

## Workspaces

| Method | Path | Notes |
| --- | --- | --- |
| `POST` | `/v1/workspaces` | Requires `X-Bootstrap-Token` when enabled. Returns the raw key **once**. |
| `GET` | `/v1/me` | Current workspace. |

```bash
curl -s localhost:8000/v1/workspaces \
  -H "X-Bootstrap-Token: $X0R_BOOTSTRAP_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"name":"Acme","plan":"trial","monthly_token_quota":200000}'
```

## Documents

| Method | Path | Notes |
| --- | --- | --- |
| `POST` | `/v1/documents` | JSON body: `title`, `content`, optional `source`, `mime`, `meta`, `embed`. |
| `POST` | `/v1/documents/upload` | `multipart/form-data`; UTF-8 text files; optional `?title=`. |
| `GET` | `/v1/documents` | `?limit=` 1–500. Includes `chunk_count`. |
| `DELETE` | `/v1/documents/{id}` | Removes the document and its chunks. |

```bash
curl -s localhost:8000/v1/documents \
  -H "Authorization: Bearer $X0R_KEY" -H "Content-Type: application/json" \
  -d '{"title":"Refund policy","content":"Full refund within 30 days..."}'
```

## Retrieval

| Method | Path | Notes |
| --- | --- | --- |
| `POST` | `/v1/search` | `{ "query": "...", "k": 5 }`. Returns scored passages with document ids. |

## Agents

| Method | Path | Notes |
| --- | --- | --- |
| `POST` | `/v1/agents` | `name`, `system_prompt`, `tools`, `model`, `temperature`, `max_steps`. |
| `GET` | `/v1/agents` | List. |
| `GET` | `/v1/agents/{id}` | Fetch. |
| `DELETE` | `/v1/agents/{id}` | Delete. |
| `POST` | `/v1/agents/{id}/run` | `{ "input": "..." }` → run with transcript. |

Built-in tools: `knowledge_search`, `calculator`, `http_get` (operator-allowlisted).

```bash
curl -s localhost:8000/v1/agents \
  -H "Authorization: Bearer $X0R_KEY" -H "Content-Type: application/json" \
  -d '{
    "name": "Support assistant",
    "system_prompt": "Answer only from the knowledge base.",
    "tools": ["knowledge_search"],
    "temperature": 0.1,
    "max_steps": 5
  }'
```

A run returns:

```json
{
  "id": "run_...",
  "status": "succeeded",
  "output": "...",
  "steps": [
    {"type": "model", "tool_calls": [{"name": "knowledge_search", "arguments": {"query": "..."}}]},
    {"type": "tool", "tool": "knowledge_search", "result": {"count": 3, "results": ["..."]}},
    {"type": "model", "content": "..."}
  ],
  "prompt_tokens": 812,
  "completion_tokens": 140,
  "latency_ms": 1180
}
```

## Runs

| Method | Path | Notes |
| --- | --- | --- |
| `GET` | `/v1/runs` | `?limit=` 1–200, newest first. |
| `GET` | `/v1/runs/{id}` | Full transcript. |

## Evals

| Method | Path | Notes |
| --- | --- | --- |
| `POST` | `/v1/evals` | `{ agent_id, name, cases: [{ input, expected }] }`. |
| `GET` | `/v1/evals` | List suites. |
| `POST` | `/v1/evals/{id}/run` | `{ "threshold": 0.45 }` → per-case scores and pass rate. |

Scoring is recall of the expected terms (see `services/evals.py`); swap in an LLM judge
behind the same contract when semantic grading matters.

## Usage

| Method | Path | Notes |
| --- | --- | --- |
| `GET` | `/v1/usage` | Current period, quota, remaining, percent, breakdown by kind. |

Starting a run after recorded usage reaches `monthly_token_quota` returns `429` with `X-Quota-Used` and `X-Quota-Limit`
headers. The check happens **before** the model call, so a blocked run costs nothing.

## Errors

| Status | Meaning |
| --- | --- |
| `401` | Missing, malformed, or revoked key |
| `403` | Workspace creation without a valid bootstrap token |
| `404` | Not found **or** belongs to another workspace |
| `413` | Upload exceeds `X0R_MAX_UPLOAD_BYTES` |
| `415` | Upload is not UTF-8 text |
| `429` | Monthly token quota exhausted |
| `502` | The agent run failed; body includes `run_id` for inspection |

## Execution policy outcomes

The run response may return `blocked` for a tool policy denial or `limit_exceeded` for a step/tool-call limit. These are HTTP 200 run records with a non-success status; clients must inspect `status`. Policy decisions appear as `type: policy` steps. Existing tools must be permitted by both the agent and `X0R_OPERATOR_TOOL_ALLOWLIST`; HTTP additionally needs the host allowlist. See [SECURITY-MODEL.md](SECURITY-MODEL.md) for behavior and limitations.
