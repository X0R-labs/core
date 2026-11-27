# Direction: X0R Guard

Status: plan. Phases 1 to 3b are implemented; later phases are proposals, not shipped behavior.

## The idea

Model gateways (LiteLLM, Portkey, Kong, Cloudflare and others) already route calls to many
providers and handle keys, budgets and usage. We don't build another one.

We build the part they leave open: **checking what an agent is allowed to do.** Before a tool
call runs, the guard answers one question:

> Can this agent take this action, for this caller, with these arguments?

Every answer is logged with its reason, the caller when the app gives one, and the tool-call
id when the model sends one. The record is saved before the action runs.

## Shape

The guard is its own small piece with no dependency on any gateway, model SDK or database.
Connectors are thin adapters that find tool calls wherever they appear and ask the guard.

```
                 ┌──────────────────────────────┐
                 │  X0R Guard                   │
                 │  rules · decisions · reasons │
                 │  check_action(request)       │
                 └──────────────┬───────────────┘
                                │
     ┌──────────────┬───────────┴──────┬─────────────────┐
  Core agents   Python SDK wrapper  LiteLLM connector   MCP proxy
  (today)       (today)             (phase 4)           (phase 4)
```

- Same rules everywhere: a policy written once applies in every connector.
- No lock-in: if a gateway changes, only its connector changes. We never fork one.
- Customers keep the gateway and provider they already use.

## Phases

| Phase | Scope | State |
| --- | --- | --- |
| 1 | Schema-driven tool policy (custom tools supported); append-only `policy_decisions` log with `GET /v1/policy/decisions` | Implemented |
| 2 | Guard extracted to `x0r_core.guard`: standard library only, no imports from tools, models, providers or the web app. `x0r_core.policy.authorize_tool` stays as a wrapper | Implemented |
| 3a | Caller identity on runs (subject and attributes, vouched for by the API key) passed to tool rules; decision committed before the tool runs; decision linked to the tool-call ID | Implemented |
| 3b | Python SDK wrapper (`x0r_core.guard.wrap`) that checks tool calls returned by any OpenAI-compatible client before the app runs them. See [SDK.md](SDK.md) | Implemented |
| 4 | Connectors driven by the first customers: LiteLLM guardrail hook, MCP proxy in front of tool servers, with signed caller identity across the hop. Per-workspace policy, retention and redaction controls, tamper-evident decision log | Proposed |

Each phase lands as its own pull request with tests and keeps the existing API working.

## Who it is for

Small and medium businesses, SaaS teams and agencies whose AI agents can take real actions:
writing to a CRM, a ticket system, a database or an outside API. Agencies matter most, because
one agency brings many client projects.

It pairs with the security review: the review finds the risky action, the guard enforces the
rule that stops it, and a test proves the bad action is blocked while the good one still works.

## Constraints every phase keeps

- Policy reason codes stay stable; a change in decision semantics bumps the policy version.
- `x0r security-demo` keeps passing and its expected output is updated in the same change.
- Commit `8538e2d` stays reproducible: the public sample review and demo instructions pin it.
- Claims stay matched to evidence: mock-provider results show orchestration, not real-model
  security, and nothing here is a client engagement, audit or security guarantee.
- Argument values are not written to the decision log.
- The guard package imports only the standard library.
- Apache-2.0; no new required runtime service for the SQLite demo path.
