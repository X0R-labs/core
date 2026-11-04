from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Chunk, Document, UsageEvent, Workspace
from ..providers import Provider, build_provider
from .chunking import approx_tokens, chunk_text


async def ingest_document(
    session: AsyncSession,
    workspace: Workspace,
    *,
    title: str,
    content: str,
    source: str = "api",
    mime: str = "text/plain",
    meta: dict | None = None,
    provider: Provider | None = None,
    embed: bool = True,
) -> tuple[Document, int]:
    """Store a document, split it, and embed its chunks when a provider allows."""
    active = provider or build_provider()

    document = Document(
        workspace_id=workspace.id,
        title=title.strip()[:300] or "Untitled",
        source=source[:300],
        mime=mime[:120],
        content=content,
        meta=meta or {},
    )
    session.add(document)
    await session.flush()

    pieces = chunk_text(content)
    vectors: list[list[float]] | None = None
    if embed and pieces:
        try:
            vectors = await active.embed(pieces)
            if vectors is not None and len(vectors) != len(pieces):
                vectors = None
        except Exception:  # noqa: BLE001 - embedding is best-effort
            vectors = None

    for i, piece in enumerate(pieces):
        session.add(
            Chunk(
                document_id=document.id,
                workspace_id=workspace.id,
                ordinal=i,
                content=piece,
                tokens=approx_tokens(piece),
                embedding=vectors[i] if vectors else None,
            )
        )

    if pieces:
        session.add(
            UsageEvent(
                workspace_id=workspace.id,
                kind="embedding_tokens",
                quantity=sum(approx_tokens(p) for p in pieces) if vectors else 0,
                meta={"document_id": document.id, "chunks": len(pieces)},
            )
        )

    await session.flush()
    return document, len(pieces)
