# Direction: Core as a secure AI gateway

Status: plan. Phase 1 is implemented; later phases are proposals, not shipped behavior.

## Why

Core already has most of what a gateway between applications and models needs: workspace
API keys, any OpenAI-compatible model endpoint, per-workspace token metering, and a policy
check before every tool call. Today those pieces sit underneath one application (document
search plus agents). The plan makes the gateway the product and the agent features one
application built on it.

```
  apps ───────────▶  gateway  ────────────▶  model endpoints
  (agents, search,   keys · quotas · policy    (OpenAI-compatible:
   evals, yours)     decision log · metering    hosted or self-hosted)
```

## Phases

| Phase | Scope | State |
| --- | --- | --- |
| 1 | Schema-driven tool policy (custom tools supported, same reason codes); append-only `policy_decisions` log with `GET /v1/policy/decisions` | Implemented |
| 2 | Package split: `x0r_core.gateway` (providers, keys, metering, policy, decision log) and `x0r_core.apps` (ingest, retrieval, agents, evals). Old import paths re-export for one release | Proposed |
| 3 | Model gateway endpoint: OpenAI-compatible chat completions through the gateway, with workspace keys, quotas, a per-workspace model allowlist and metering | Proposed |
| 4 | Policy per workspace instead of only process-wide settings; retention and redaction controls for stored transcripts; tamper-evident decision log | Proposed |

Each phase lands as its own pull request with tests, and keeps the existing API working.

## Constraints every phase keeps

- Policy reason codes stay stable; a change in decision semantics bumps the policy version.
- `x0r security-demo` keeps passing and its expected output is updated in the same change.
- Commit `8538e2d` stays reproducible: the public sample review and demo instructions pin it.
- Claims stay matched to evidence: mock-provider results show orchestration, not real-model
  security, and nothing here is a client engagement, audit or security guarantee.
- Argument values are not written to the decision log.
- Apache-2.0; no new required runtime service for the SQLite demo path.
