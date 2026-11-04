# Sample review: operator permission on a synthetic knowledge lookup

**Demonstration of our own experimental X0R Core. Not a client engagement, independent audit, certification or proof of production security.** Evidence comes from deterministic mock testing. No customer data, external targets or real model calls are involved.

## Scope

One in-process workflow: synthetic question → mock tool request → execution policy → workspace knowledge lookup → mock answer. One tool, `knowledge_search`, granted to the agent in both cases. A synthetic document states a 24-month warranty. Two operator configurations are compared in the same disposable database. API authentication, deployment ingress, external HTTP and caller/resource authorization are outside this exercise.

## Threat scenario

A model-generated request asks to use a tool the operator has not permitted, even though the agent advertises it. Does the service invoke the implementation despite the denial? The mock deliberately emits the request; it is not persuaded by an adversarial prompt. This tests execution after tool selection, not real-model behavior under prompt injection.

## Evidence

Run `x0r security-demo` after installation. [Reproduction](SECURITY-REVIEW-DEMO.md) and [expected JSON](security-demo.expected.json) define the setup and checks. Record `git rev-parse HEAD` when presenting or rerunning it.

- Denied: the operator permits only `calculator`; the mock requests `knowledge_search`. Status `blocked`, reason `not_granted_by_operator`, zero handler entries and zero tool execution steps.
- Permitted: the operator adds `knowledge_search`. Status `succeeded`, reason `granted`, one entry to the wrapped real handler and an answer containing `24 months` from the synthetic document.
- Source: `services/agents.py` checks the decision before handler dispatch; `policy.py` checks operator and agent grants. Tests check handler entry directly in addition to recorded steps.

## Findings and remediation

| Observation | Interpretation | Remediation / next action |
| --- | --- | --- |
| Denied lookup never enters the handler in this case | The examined operator boundary works as configured; no bypass reproduced | Preserve pre-dispatch enforcement and the regression in CI |
| Permitted lookup returns synthetic content | An allowed operation is not necessarily appropriate for every caller or resource | Map caller identity and implement/test resource permissions before sensitive use |
| Policy events omit arguments, but other run records retain raw content | Sensitive trace content remains a risk | Use synthetic/redacted data, restrict database access and define retention/deletion before sensitive use |

No severity score or client-wide risk rating is assigned from this exercise. Remaining risks are documented design limitations, not newly reproduced exploits. The permitted lookup is an expected control case, not evidence of unauthorized disclosure.

## Limitations

- Operator-wide and agent-level grants are not end-user RBAC. Workspace/cross-workspace checks have separate tests; this demo does not exercise them.
- Mock selection is deterministic. It does not show real-model robustness, output accuracy, safe use of every permitted operation or that prompt injection is solved.
- The counter measures entry to the registered handler in this process. It is not OS-level side-effect monitoring or independent forensic evidence. Policy records are not tamper-evident; the summary is not a full transcript or proof of causal attribution.
- With a real provider, a tool policy denial follows a model call and can incur usage. Quota admission checks are separate and are not atomic reservations or hard spending caps.
- HTTP is disabled here. DNS checks and connections remain separate; rebinding/TOCTOU and enforced egress are unresolved. Calculator bounds are not process isolation. Input/step/tool-count limits do not replace ingress/concurrency controls.
- In-process SQLite/mock testing does not validate deployment, secrets handling, production load, network isolation or provider data flows. A real review must agree scope, authorization, safe data and actual deployment evidence.
