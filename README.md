# X0R Core

X0R checks what AI agents are allowed to do before they do it.

## Why

- Decide which tools an agent can use and what it can do with them.
- Block calls that break your rules before the tool runs.
- See which calls were allowed or blocked and why.

## How it fits

| Connect with | What it checks | Status |
| --- | --- | --- |
| SDK `wrap()` | Tool calls returned to your Python app by an OpenAI-compatible client | Available |
| Core API | Tool calls in agents run by your self-hosted Core server | Available |
| MCP proxy | Tool calls sent to MCP servers | In progress |
| LiteLLM gateway | Tool calls in model replies routed through LiteLLM | Planned |

Core also handles document ingestion and search, run transcripts, token usage, and evals. See [Internals](docs/INTERNALS.md).

## Quick start

**Use Guard in your app.** Install it with `pip install x0r-guard` after the first release. For now, run `pip install ./packages/x0r-guard` from this checkout.

Use your existing OpenAI-compatible `client`, `model`, and `tools` list with a `lookup_order` tool. Run from the checkout to load [the example policy](examples/policy.toml). Use the Core workspace API key from the [getting started guide](docs/GETTING-STARTED.md) as `core_api_key`. Core records allowed and blocked calls and their reasons in the workspace decision log.

```python
from x0r_guard import Caller, Policy, core_sink, wrap

policy = Policy.from_file("examples/policy.toml",
                          on_decision=core_sink("http://localhost:8000", core_api_key))
guarded = wrap(client, policy)
reply = guarded.chat.completions.create(
    model=model,
    messages=[{"role": "user", "content": "Look up order A-1."}],
    tools=tools,
    x0r_caller=Caller("user-42", {"orders": "A-1"}),
)
tool_calls = reply.choices[0].message.tool_calls
```

**Run Core.** From the checkout, start Core and Postgres. Compose uses the offline mock provider by default.

Copy [`.env.example`](.env.example) to `.env` if you haven’t already. Set `X0R_CALLER_KEY_SECRET` and `X0R_BOOTSTRAP_TOKEN` in `.env`, using a separate `openssl rand -hex 32` value for each. Reuse those values across restarts. Changing the caller key secret invalidates existing caller tokens. Changing the bootstrap token makes the old token stop working.

Compose reads `.env` for variable substitution and passes the settings listed in `docker-compose.yml` to Core. Exported shell variables take precedence.

```bash
docker compose up --build -d
```

Open `http://localhost:8000/docs`. The [getting started guide](docs/GETTING-STARTED.md) shows how to create a workspace and upload a policy.

For a local SQLite demo, run `pip install -e packages/x0r-guard -e ".[dev]"`, then `x0r seed` and `x0r serve`. For a real model, copy `.env.example` to `.env` if needed, then edit `.env` to set `X0R_PROVIDER=openai_compatible` and configure the endpoint, API key, and chat model. When running Core directly, it also reads the embedding model setting from `.env`. See the [security model](docs/SECURITY-MODEL.md) before deployment.

## Progress

See the [Phase 4 plan](docs/PHASE-4-PLAN.md).

- [x] 4a. Signed caller identity
- [x] 4b. Policy files and shared decision log
- [ ] 4c. MCP guard proxy (in progress)
- [ ] 4d. LiteLLM connector (planned)
- [ ] 4e. Hash-chained decision log (planned)
- [ ] 4f. Retention and redaction (planned)
- [ ] 4g. Key control (planned)
- [ ] 4h. AI access inventory (planned)

## Docs

- [Getting started](docs/GETTING-STARTED.md) · [SDK](docs/SDK.md) · [API](docs/API.md) · [Caller tokens](docs/CALLER-TOKENS.md)
- [Security model](docs/SECURITY-MODEL.md) · [Architecture](docs/ARCHITECTURE.md) · [Internals](docs/INTERNALS.md) · [Gateway plan](docs/GATEWAY-PLAN.md)
- [Security demo](docs/SECURITY-REVIEW-DEMO.md) · [Sample security review](docs/SAMPLE-SECURITY-REVIEW.md)

## Contributing and license

Run `pytest -q` and `x0r security-demo` after changes. [Apache-2.0](LICENSE). Built by [X0R Labs](https://x0r-labs.github.io/site/).
