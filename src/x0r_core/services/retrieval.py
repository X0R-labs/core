from __future__ import annotations

import math
import re
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Chunk, Document
from ..providers import Provider, build_provider

_WORD = re.compile(r"[a-z0-9]+")


def _terms(text: str) -> list[str]:
    return _WORD.findall(text.lower())


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


@dataclass
class Hit:
    chunk_id: str
    document_id: str
    title: str
    text: str
    score: float

    def as_dict(self) -> dict:
        return {
            "chunk_id": self.chunk_id,
            "document_id": self.document_id,
            "title": self.title,
            "text": self.text,
            "score": round(self.score, 4),
        }


async def search(
    session: AsyncSession,
    workspace_id: str,
    query: str,
    *,
    k: int = 5,
    provider: Provider | None = None,
) -> list[Hit]:
    """Hybrid retrieval: vector similarity when embeddings exist, keyword otherwise.

    Both paths are combined, so a workspace that was ingested before an embedding
    provider was configured still returns useful results.
    """
    rows = (
        await session.execute(
            select(Chunk, Document)
            .join(Document, Chunk.document_id == Document.id)
            .where(Chunk.workspace_id == workspace_id)
            .limit(20_000)
        )
    ).all()

    if not rows:
        return []

    query_terms = _terms(query)
    query_vec: list[float] | None = None
    any_embeddings = any(chunk.embedding for chunk, _ in rows)
    if any_embeddings and query_terms:
        try:
            active = provider or build_provider()
            vectors = await active.embed([query])
            query_vec = vectors[0] if vectors else None
        except Exception:  # noqa: BLE001 - retrieval must never fail a run
            query_vec = None

    hits: list[Hit] = []
    for chunk, document in rows:
        keyword = _keyword_score(query_terms, chunk.content)
        vector = cosine(query_vec, chunk.embedding) if query_vec and chunk.embedding else 0.0
        score = (0.65 * vector + 0.35 * keyword) if query_vec else keyword
        if score <= 0:
            continue
        hits.append(
            Hit(
                chunk_id=chunk.id,
                document_id=document.id,
                title=document.title,
                text=chunk.content,
                score=score,
            )
        )

    hits.sort(key=lambda h: h.score, reverse=True)
    return hits[:k]


def _keyword_score(query_terms: list[str], text: str) -> float:
    if not query_terms:
        return 0.0
    doc_terms = _terms(text)
    if not doc_terms:
        return 0.0
    doc_counts: dict[str, int] = {}
    for t in doc_terms:
        doc_counts[t] = doc_counts.get(t, 0) + 1

    score = 0.0
    for term in set(query_terms):
        tf = doc_counts.get(term, 0)
        if tf:
            # Saturating term frequency, damped by chunk length.
            score += (tf / (tf + 1.2)) * (1.0 / (1.0 + len(doc_terms) / 400))
    return score / (len(set(query_terms)) or 1)
