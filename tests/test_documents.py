from __future__ import annotations

from x0r_core.services.chunking import chunk_text

POLICY = (
    "Refunds are available within 30 days of delivery. After 30 days, refunds are prorated "
    "at 50 percent up to day 60.\n\n"
    "Damaged goods must be reported within 7 days with photos.\n\n"
    "Express delivery takes 1 to 2 business days. Standard delivery takes 3 to 5 business days."
)


async def test_create_document_and_search(client):
    resp = await client.post(
        "/v1/documents",
        json={"title": "Policy", "content": POLICY},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["chunks"] >= 1
    assert body["document"]["chunk_count"] == body["chunks"]

    search = await client.post("/v1/search", json={"query": "how long does express delivery take"})
    assert search.status_code == 200
    results = search.json()["results"]
    assert results
    assert "express" in results[0]["text"].lower()


async def test_list_and_delete_document(client):
    created = await client.post("/v1/documents", json={"title": "Doc", "content": POLICY})
    doc_id = created.json()["document"]["id"]

    listed = await client.get("/v1/documents")
    assert any(d["id"] == doc_id for d in listed.json())

    deleted = await client.delete(f"/v1/documents/{doc_id}")
    assert deleted.status_code == 204

    missing = await client.delete(f"/v1/documents/{doc_id}")
    assert missing.status_code == 404


async def test_upload_text_file(client):
    resp = await client.post(
        "/v1/documents/upload",
        files={"file": ("notes.txt", b"Warranty lasts 24 months.", "text/plain")},
    )
    assert resp.status_code == 201
    assert resp.json()["document"]["source"] == "notes.txt"


async def test_upload_rejects_binary(client):
    resp = await client.post(
        "/v1/documents/upload",
        files={"file": ("blob.bin", b"\xff\xfe\x00\x01", "application/octet-stream")},
    )
    assert resp.status_code == 415


async def test_search_on_empty_workspace(client):
    resp = await client.post("/v1/search", json={"query": "anything"})
    assert resp.status_code == 200
    assert resp.json()["results"] == []


def test_chunking_is_stable_and_bounded():
    text = "\n\n".join([f"Paragraph number {i} with some words." for i in range(80)])
    chunks = chunk_text(text, target_chars=300)
    assert len(chunks) > 1
    assert all(len(c) <= 500 for c in chunks)
    assert "Paragraph number 0" in chunks[0]
