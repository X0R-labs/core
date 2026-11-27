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

Configure your MCP client to launch `x0r mcp-proxy` in front of your local
stdio server. Start from the [Claude Desktop example](../examples/mcp/claude_desktop_config.json)
and replace the paths and command with your own. Use a
[TOML policy](../examples/policy.toml) to specify which tools and arguments employees
can use.

Check the policy against the server's tool schemas before using the proxy.

```bash
x0r mcp-proxy --check --policy /path/to/policy.toml -- orders-mcp
```

The check lists each tool's allow-list and schema status. It exits with status 1
when an allowed tool uses a schema the guard cannot check.

For live use, specify `--decision-log /path/to/decisions.jsonl` to save decisions
locally, `--core-url https://core.example.com` with `X0R_API_KEY` to save them to
Core, or both. A call is denied if a configured destination fails to save the decision.
Use `--block-resources` or `--block-prompts` to disable those MCP methods.
The proxy denies tool calls that include arguments absent from the tool's declared input schema.
A line larger than 8 MB from either the client or the server ends the proxy session.

If your policy uses caller rules, as the sample TOML policy does, set
`X0R_CALLER_KEY` to the base64url audience verification key in the MCP client's
environment and add `--caller-token-file /path/to/token` before `--`. Issue
[caller tokens](CALLER-TOKENS.md) for the `mcp` audience or set `--audience` to
match your issuer. The proxy reads the file before every tool call. A client retry
after a lost response can run the tool again.

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
