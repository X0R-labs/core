# Getting started with X0R

The Python packages and Docker image will be available on PyPI and GHCR after the
first tagged release. Until then, use the Git commands below.

## Developers

Install the guard with `pip install x0r-guard`. Before the first release, install it
directly from Git:

```bash
pip install 'x0r-guard @ git+https://github.com/X0R-labs/core.git#subdirectory=packages/x0r-guard'
```

Wrap your OpenAI-compatible client to check tool calls before running them. This
example uses an existing `client`, `model`, and `tools` list with a `lookup_order` tool.

```python
from x0r_guard import Caller, Policy, wrap

guarded = wrap(client, Policy(allow=["lookup_order"]))
reply = guarded.chat.completions.create(
    model=model,
    messages=[{"role": "user", "content": "Look up order A-1."}],
    tools=tools,
    x0r_caller=Caller("user-42"),
)
tool_calls = reply.choices[0].message.tool_calls
```

See the [SDK guide](SDK.md) for policy settings and a runnable example.

## Platform and IT teams

Set these secrets before starting Core. Keep the caller key secret stable across
restarts so existing caller tokens can still be verified.

```bash
export X0R_CALLER_KEY_SECRET="$(openssl rand -hex 32)"
export X0R_BOOTSTRAP_TOKEN="$(openssl rand -hex 32)"
```

After the first release, run the image from GHCR:

```bash
docker run --rm -p 127.0.0.1:8000:8000 \
  -e X0R_CALLER_KEY_SECRET -e X0R_BOOTSTRAP_TOKEN \
  -e X0R_DATABASE_URL=sqlite+aiosqlite:////data/x0r.db \
  -v x0r-data:/data ghcr.io/x0r-labs/core:0.1.0
```

Before the first release, build Core from Git with Docker Compose:

```bash
git clone https://github.com/X0R-labs/core.git
cd core
docker compose up --build -d
```

To run Core from the same checkout without Docker, use
`pip install ./packages/x0r-guard .` and then `x0r serve`.

From a shell with the same bootstrap token, create a workspace. Save the API key
from the response. Core returns it only once.

```bash
curl -fsS http://localhost:8000/v1/workspaces \
  -H "X-Bootstrap-Token: $X0R_BOOTSTRAP_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"name":"Acme","plan":"trial","monthly_token_quota":200000}'
```

Set `X0R_KEY` to that API key. From the repository checkout, upload the example
workspace policy with `jq` and `curl`.

```bash
jq -Rs '{policy: .}' examples/policy.toml | curl -fsS -X PUT \
  http://localhost:8000/v1/policy \
  -H "Authorization: Bearer $X0R_KEY" \
  -H "Content-Type: application/json" \
  --data-binary @-
```

Use a file whose tool names match those in your app or Core agents. Core runs require
the file's allowlist and rules in addition to the existing agent and operator checks.
The response includes the digest and revision. `GET /v1/policy` returns the current file.
Each run uses the revision that was current when it started.

Use the same TOML file in your SDK app and pass the policy to `wrap()` to send its
decisions to this workspace.

```python
import os

from x0r_guard import Policy, core_sink

policy = Policy.from_file(
    "examples/policy.toml",
    on_decision=core_sink("http://localhost:8000", os.environ["X0R_KEY"]),
)
```

View both Core and SDK decisions in the workspace log.

```bash
curl -fsS http://localhost:8000/v1/policy/decisions \
  -H "Authorization: Bearer $X0R_KEY"
```

See [Caller tokens](CALLER-TOKENS.md) to set up signed caller identities.

## Employees using MCP tools

Configure your MCP client to launch `x0r mcp-proxy`. For a local stdio server, start
from the [Claude Desktop example](../examples/mcp/claude_desktop_config.json)
and replace the paths and command with your own. Use a
[TOML policy](../examples/policy.toml) to specify which tools and arguments employees
can use.

Check the policy against the server's tool schemas before using the proxy.

```bash
x0r mcp-proxy --check --policy /path/to/policy.toml -- orders-mcp
```

For a Streamable HTTP server, the MCP client still uses stdio. Pass the endpoint
with `--upstream` in place of the server command.

```bash
x0r mcp-proxy --policy /path/to/policy.toml --decision-log /path/to/decisions.jsonl \
  --upstream https://orders.example.com/mcp --upstream-token-env ORDERS_MCP_TOKEN
```

HTTPS is required except for localhost and loopback IP addresses. If the upstream
requires a bearer token, set the environment variable named by `--upstream-token-env`.
For an HTTP policy check, use `--check --upstream URL` with the same token option if needed.

The check lists each tool's allow-list and schema status. It exits with status 1
when an allowed tool uses a schema the guard cannot check.

For live use, specify `--decision-log /path/to/decisions.jsonl` to save decisions
locally, `--core-url https://core.example.com` with `X0R_API_KEY` to save them to
Core, or both. A call is denied if a configured destination fails to save the decision.
Use `--block-resources` or `--block-prompts` to disable those MCP methods.
The guard supports typed arrays, nested objects and local schema references up to
five levels deep, alongside scalar types and constraints. Every object rejects
undeclared keys, including with `additionalProperties: true`. Stdio messages
larger than 8 MB end the proxy session. See the
[guard schema rules](SECURITY-MODEL.md) for details.

If your policy uses caller rules, as the sample TOML policy does, set
`X0R_CALLER_KEY` to the base64url audience verification key in the MCP client's
environment and add `--caller-token-file /path/to/token` to the proxy options. Issue
[caller tokens](CALLER-TOKENS.md) for the `mcp` audience or set `--audience` to
match your issuer. The proxy reads the file before every tool call.

For safe retries within a proxy session, send a string of 1 to 200 characters in
`tools/call`'s `params._meta.idempotencyKey`. For the same caller, tool and
arguments, the proxy checks policy again, records the decision and replays the
stored response with the retry's request ID. Reusing the key for a different call
is denied with `idempotency_key_reused`. A matching call still in flight is
denied with `idempotency_in_progress`. If the upstream outcome is unknown or the
response body exceeds 1 MiB, a retry is denied with
`idempotency_outcome_unknown` or `idempotency_result_too_large`, respectively.
The proxy keeps replay entries for up to 10 minutes, evicting the oldest first
to stay within 1,000 entries and 32 MiB of stored JSON bodies. Replay memory
lives in the proxy process. Restarting the proxy forgets the keys.

## Releasing

1. In GitHub, create the `pypi` environment under **Settings → Environments**.
2. On [PyPI](https://pypi.org/manage/account/publishing/), add a pending trusted
   publisher for each project, `x0r-guard` and `x0r-core`. For both, enter owner
   `X0R-labs`, repository `core`, workflow `release.yml`, and environment `pypi`.
3. Set both package versions to the release version and tag the release commit:

   ```bash
   git tag v0.1.0 && git push origin v0.1.0
   ```

   The workflow publishes both packages to PyPI and tags the GHCR image with
   the version and `latest`. For anonymous pulls, set the GHCR package visibility
   to public after the first push.
