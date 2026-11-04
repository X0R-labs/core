from __future__ import annotations

import re

_WS = re.compile(r"\s+")


def normalize(text: str) -> str:
    return _WS.sub(" ", text).strip()


def chunk_text(text: str, target_chars: int = 900, overlap: int = 140) -> list[str]:
    """Paragraph-aware chunking with a character budget and overlap.

    Small enough for precise retrieval, large enough to keep context coherent.
    """
    text = text.replace("\r\n", "\n").strip()
    if not text:
        return []

    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    if not paragraphs:
        paragraphs = [text]

    chunks: list[str] = []
    current = ""

    def flush() -> None:
        nonlocal current
        if current.strip():
            chunks.append(current.strip())
        current = ""

    for para in paragraphs:
        if len(para) > target_chars * 3:
            flush()
            for i in range(0, len(para), target_chars):
                piece = para[i : i + target_chars]
                if piece.strip():
                    chunks.append(piece.strip())
            continue

        candidate = f"{current}\n\n{para}".strip() if current else para
        if len(candidate) <= target_chars:
            current = candidate
        else:
            flush()
            tail = chunks[-1][-overlap:] if chunks else ""
            current = f"{tail}\n\n{para}".strip() if tail else para

    flush()

    # Merge a runt final chunk into its predecessor.
    if len(chunks) > 1 and len(chunks[-1]) < target_chars // 4:
        chunks[-2] = f"{chunks[-2]}\n\n{chunks[-1]}"
        chunks.pop()

    return chunks


def approx_tokens(text: str) -> int:
    return max(1, len(text) // 4)
