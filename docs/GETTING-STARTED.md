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

Set `X0R_KEY` to that API key and view the workspace's policy decisions. The log
starts empty and fills as Core checks tool calls.

```bash
curl -fsS http://localhost:8000/v1/policy/decisions \
  -H "Authorization: Bearer $X0R_KEY"
```

See [Caller tokens](CALLER-TOKENS.md) to set up signed caller identities.

## Employees using MCP tools

MCP support is coming in [phase 4c](PHASE-4-PLAN.md). A planned configuration for
a local MCP server looks like this:

```json
{
  "mcpServers": {
    "orders": {
      "command": "x0r",
      "args": ["mcp-proxy", "--policy", "/path/to/policy.toml", "--", "orders-mcp"]
    }
  }
}
```

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
