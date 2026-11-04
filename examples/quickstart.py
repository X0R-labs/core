"""End-to-end example against a running X0R Core.

    x0r seed && x0r serve          # terminal 1
    X0R_KEY=... python examples/quickstart.py   # terminal 2
"""

from __future__ import annotations

import os

import httpx

BASE = os.environ.get("X0R_BASE_URL", "http://localhost:8000")
KEY = os.environ["X0R_KEY"]
HEADERS = {"Authorization": f"Bearer {KEY}"}


def main() -> None:
    with httpx.Client(base_url=BASE, headers=HEADERS, timeout=60) as client:
        me = client.get("/v1/me").json()
        print(f"workspace: {me['name']} ({me['id']})")

        doc = client.post(
            "/v1/documents",
            json={
                "title": "Warranty policy",
                "content": (
                    "All hardware carries a 24-month warranty against manufacturing "
                    "defects.\n\nConsumables are excluded. Warranty claims require proof "
                    "of purchase and are processed within 10 business days."
                ),
            },
        ).json()
        print(f"ingested: {doc['document']['title']} → {doc['chunks']} chunk(s)")

        hits = client.post("/v1/search", json={"query": "how long is the warranty"}).json()
        for hit in hits["results"][:3]:
            print(f"  [{hit['score']:.3f}] {hit['title']}: {hit['text'][:80]}…")

        agent = client.post(
            "/v1/agents",
            json={
                "name": "Warranty assistant",
                "system_prompt": "Answer strictly from the knowledge base.",
                "tools": ["knowledge_search"],
            },
        ).json()

        run = client.post(
            f"/v1/agents/{agent['id']}/run",
            json={"input": "Is the warranty longer than two years?"},
        ).json()
        print(f"\nrun {run['id']} → {run['status']} in {run['latency_ms']}ms")
        print(run["output"])

        usage = client.get("/v1/usage").json()
        print(f"\ntokens this month: {usage['llm_tokens_used']}/{usage['monthly_token_quota']}")


if __name__ == "__main__":
    main()
