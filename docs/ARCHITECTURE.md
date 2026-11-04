# Architecture

```
                 ┌──────────────────────────────────────────────┐
  client ──────▶ │  FastAPI  /v1/*                              │
                 │  auth → workspace scope → handler            │
                 └───────┬───────────────┬──────────────────────┘
                         │               │
                 ┌───────▼──────┐ ┌──────▼────────┐
                 │  services    │ │  providers    │
                 │  ingest      │ │  mock         │
                 │  retrieval   │ │  openai-      │
                 │  agents      │ │  compatible   │
                 │  evals       │ └──────┬────────┘
                 │  usage       │        │
                 └───────┬──────┘        ▼
                         │        OpenAI / OpenRouter /
                 ┌───────▼──────┐  Groq / vLLM / Ollama
                 │ SQLAlchemy   │
                 │ SQLite | PG  │
                 └──────────────┘
```

## Request lifecycle of an agent run

1. `POST /v1/agents/{id}/run` authenticates the key, hashes it, and scopes everything
   to the key's workspace.
2. `usage.assert_within_quota` refuses the run before any model spend if the workspace
   has exhausted its monthly `llm_tokens`.
3. The loop sends the system prompt, the user input, and the agent's tool schemas to the
   provider.
4. If the model requests tools, each one runs with a `ToolContext(session, workspace_id,
   settings)` — so `knowledge_search` can only ever read the caller's own chunks.
5. Tool results go back into the transcript. The loop repeats until the model answers or
   hits `max_steps`.
6. The full transcript, token counts, and latency are written to `runs`; a `usage_events`
   row records the spend.

Every failure mode is captured rather than thrown away: a failed run is persisted with
`status="failed"` and its error, then surfaced as `502` with the `run_id` so the caller can
inspect exactly what happened.

## Data model

| Table | Purpose |
| --- | --- |
| `workspaces` | Tenant, plan, monthly token quota |
| `api_keys` | SHA-256 of the key; prefix kept for lookup; revocable |
| `documents` | Original content + metadata |
| `chunks` | Retrieval unit; content, ordinal, approximate tokens, embedding |
| `agents` | Prompt, model, tool allowlist, temperature, step limit |
| `runs` | Input, output, step transcript, tokens, latency, error |
| `usage_events` | Append-only meter (`llm_tokens`, `embedding_tokens`) |
| `eval_suites` / `eval_cases` | Golden questions, last output, score, pass |

## Retrieval

`search()` is hybrid by design:

- If the workspace has embeddings and the provider can embed the query: `0.65 * cosine +
  0.35 * keyword`.
- If not: keyword score alone.

This means a workspace ingested before an embedding key was configured still works, and
upgrading the embedding model never makes old documents unsearchable. The current
implementation loads the workspace's chunks and scores in-process, which is honest and
fast up to roughly 10⁴–10⁵ chunks.

### Scaling path

1. **Vectors → pgvector.** Store `vector(1536)` in `chunks`, add an HNSW index, and let
   Postgres do ANN. The `Hit` contract does not change.
2. **Keyword → Postgres FTS.** `tsvector` + `websearch_to_tsquery`, merged with the ANN
   results by reciprocal rank fusion.
3. **Ingestion → queue.** Move chunking + embedding to a worker (Redis/RQ, Arq, or SQS)
   and make `POST /v1/documents` return a job id.
4. **Sessions → stateless.** The API is already stateless apart from the DB.

## Security posture

- API keys are never stored in plaintext. Only a SHA-256 digest and a short prefix are
  persisted.
- Every query is scoped by `workspace_id`; cross-tenant lookups return `404`, not `403`,
  so IDs cannot be probed.
- `calculator` is an AST walker with an operator allowlist and exponent limits — not
  `eval()`.
- `http_get` is disabled by default. When enabled it requires an exact host allowlist and
  refuses any host that resolves to a private, loopback, link-local, reserved, or
  multicast address.
- Tool output is truncated before it re-enters the model context.

## Deliberate omissions

Streaming, a UI, pgvector, Stripe, SSO. Each is a week of work, not an architectural
change. They are listed in the README roadmap because pretending v0.1 has them would be
the dishonest thing to ship.
