# X0R Core

**The AI agent backend you own.**

Self-hosted. Provider-agnostic. Metered.

X0R Core is a small, complete backend for putting AI agents on top of a company's own
documents: ingest, retrieve, run tool-calling agents, grade them, and meter every token —
without handing your data or your bill to a platform you don't control.

```bash
pip install -e ".[dev]"
x0r seed        # demo workspace: documents + agent + eval suite, no API key needed
x0r serve       # http://127.0.0.1:8000/docs
```

It runs with **zero configuration** — no API key, no database server, no Docker. The
built-in `mock` provider exercises the whole pipeline offline so you can see it work
before you spend a cent.

---

## Reproducible security demonstration

Run `x0r security-demo` after installation for **deterministic mock testing** of an
operator-denied lookup (zero handler entries) and a permitted lookup (one real handler
entry). It uses synthetic data and a disposable database, with no provider API call.
See the [demo command and expected output](docs/SECURITY-REVIEW-DEMO.md) and
[sample review](docs/SAMPLE-SECURITY-REVIEW.md). This demonstrates our own experimental
system, not a client engagement, independent audit or production security guarantee.

## Why this exists

Most teams that try to ship an AI feature end up rebuilding the same six things:
ingestion, retrieval, a tool loop, per-tenant auth, usage accounting, and a way to prove
quality didn't regress. Then they couple all of it to one vendor's SDK.

X0R Core is those six things, written once, designed to be run inside your own
infrastructure and pointed at any OpenAI-compatible model endpoint.

| Problem | What Core gives you |
| --- | --- |
| "Where do we put the documents?" | Chunking + embeddings + hybrid retrieval, in your database |
| "How do agents call our stuff?" | A bounded tool loop with a registry you can extend |
| "How do we bill tenants?" | API keys, workspaces, quotas, per-run token accounting |
| "How do we know it still works?" | Eval suites with pass rates, stored per case |
| "Can we self-host it?" | FastAPI + SQLAlchemy. SQLite for a demo, Postgres support (validate your deployment) |
| "Are we locked to OpenAI?" | Any OpenAI-compatible base URL: OpenAI, OpenRouter, Groq, vLLM, Ollama |

## What's in the box

- **Workspaces & API keys** — `x0r_<prefix>_<secret>`, hashed at rest, revocable.
- **Ingestion** — JSON or UTF-8 file upload; paragraph-aware chunking with overlap.
- **Retrieval** — hybrid vector + keyword search that degrades gracefully when
  embeddings are unavailable.
- **Agents** — system prompt, model, temperature, step limit, tool allowlist.
- **Tools** — `knowledge_search`, bounded `calculator`, and disabled-by-default `http_get`
  with host/DNS checks and known connection-validation gaps (see the security model).
- **Runs** — full transcript persisted: every model call, every tool call, every result.
- **Metering** — monthly token quota per workspace, enforced with `429`, plus a usage
  breakdown by kind.
- **Evals** — golden-question suites scored per case, with pass rate.

## Quickstart

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

cp .env.example .env
x0r seed
x0r serve
```

The seed command prints an API key. Ask the demo agent a question:

```bash
export X0R_KEY=x0r_...

curl -s localhost:8000/v1/agents/agt_.../run \
  -H "Authorization: Bearer $X0R_KEY" \
  -H "Content-Type: application/json" \
  -d '{"input":"Can I get a refund after 45 days?"}' | jq
```

You get back the answer, the retrieved sources, and the token cost:

```json
{
  "status": "succeeded",
  "output": "Here is what the knowledge base says:\n\n- Refunds are available within 30 days...",
  "steps": [
    {"type": "model", "tool_calls": [{"name": "knowledge_search"}]},
    {"type": "tool", "tool": "knowledge_search", "result": {"count": 2}},
    {"type": "model", "content": "..."}
  ],
  "prompt_tokens": 412,
  "completion_tokens": 96,
  "latency_ms": 3
}
```

## Connecting a real model

```bash
# Any OpenAI-compatible endpoint.
export X0R_PROVIDER=openai_compatible
export X0R_OPENAI_BASE_URL=https://api.openai.com/v1   # or https://openrouter.ai/api/v1
export X0R_OPENAI_API_KEY=sk-...
export X0R_CHAT_MODEL=gpt-4o-mini
export X0R_EMBEDDING_MODEL=text-embedding-3-small
x0r serve
```

Works the same against a local model:

```bash
export X0R_OPENAI_BASE_URL=http://localhost:11434/v1   # Ollama
export X0R_OPENAI_API_KEY=ollama
export X0R_CHAT_MODEL=llama3.1
```

## Production

SQLite is the demo/default. Postgres is the production path:

```bash
pip install -e ".[postgres]"
export X0R_DATABASE_URL=postgresql+asyncpg://x0r:x0r@localhost:5432/x0r
```

Docker:

```bash
docker compose up --build
```

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the design and the scaling path
(pgvector, queues, multi-region), and [`docs/API.md`](docs/API.md) for the endpoint map.

## Tests

```bash
pytest -q
```

The suite covers auth, ingestion, retrieval, the agent tool loop, tenant isolation,
quota enforcement, and eval scoring — all against the mock provider, so it runs offline
in about a second.

## What this is not (yet)

X0R Core v0.1 is a **strong foundation**, not a finished SaaS. Deliberately out of scope:

- pgvector-backed ANN search (today: in-process cosine over the workspace's chunks)
- Streaming responses and token-by-token output
- A UI (the API + OpenAPI docs are the product surface)
- Billing integration (Stripe is a thin layer over `/v1/usage`)
- SSO / RBAC beyond workspace-scoped keys

Those are the roadmap. If you need one of them for a specific deployment, that's the
conversation.

## License

Apache-2.0. Use it, fork it, run it commercially.

---

Built by [X0R Labs](https://x0r-labs.github.io/site/) — AI software, built to be the difference.

## Security iteration

Execution now checks an operator-controlled built-in tool allowlist before invoking a handler. Policy denials stop a run as `blocked`; execution limits return `limit_exceeded`. HTTP is denied by the operator policy by default, including for agents that list it. Custom tools require explicit policy implementation.

Read [the security model and known limits](docs/SECURITY-MODEL.md) before deployment. Quotas are not hard spend caps; full trace redaction, end-user RBAC and connection-bound HTTP validation remain unfinished. Use synthetic data for evaluation.

### Resource and evaluation follow-up

HTTP fetching now streams under byte/time limits and rejects compressed responses and redirects. Calculator evaluation and API inputs have explicit limits. Evaluations cannot count blocked/failed/limited runs as passes, and quota stops preserve prior completed usage records. Read [the security model](docs/SECURITY-MODEL.md) for compatibility details and the remaining DNS, privacy and concurrent-budget gaps.
