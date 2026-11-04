from __future__ import annotations

from x0r_core.services.evals import similarity

KB = (
    "Refund policy: full refund within 30 days. After 30 days refunds are prorated at 50 "
    "percent up to day 60.\n\nExpress delivery takes 1 to 2 business days."
)


def test_similarity_is_recall_of_expected_terms():
    assert similarity("express delivery takes 1 to 2 business days", "1 to 2 business days") == 1.0
    assert similarity("unrelated", "1 to 2 business days") < 0.2
    assert similarity("anything", "") == 1.0


async def test_eval_suite_runs(client):
    await client.post("/v1/documents", json={"title": "Policy", "content": KB})
    agent = (
        await client.post(
            "/v1/agents",
            json={
                "name": "eval-agent",
                "system_prompt": "Answer from the knowledge base.",
                "tools": ["knowledge_search"],
            },
        )
    ).json()

    suite = await client.post(
        "/v1/evals",
        json={
            "agent_id": agent["id"],
            "name": "golden",
            "cases": [
                {
                    "input": "Can I get a refund after 45 days?",
                    "expected": "prorated 50 percent up to day 60",
                },
                {
                    "input": "How fast is express delivery?",
                    "expected": "1 to 2 business days",
                },
            ],
        },
    )
    assert suite.status_code == 201, suite.text

    result = await client.post(
        f"/v1/evals/{suite.json()['id']}/run", json={"threshold": 0.5}
    )
    assert result.status_code == 200, result.text
    body = result.json()
    assert body["total"] == 2
    assert body["passed"] == 2
    assert body["pass_rate"] == 1.0
