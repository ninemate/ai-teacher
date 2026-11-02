"""Rebuild a document's text in reading order from the indexed chunks.

Ingestion keeps the chunk text only in the vector store (``qdrant_store.scroll_document``), each chunk with its
position, page or chapter and OCR confidence. Chunks overlap by up to ``ingest_chunk_overlap`` characters within a
page or chapter; ``trim_overlap`` removes that repetition so the text can be read, summarised or translated without
doubled sentences. Pure functions apart from ``load_chunks``; the Read/Audiobook feature needs the same ordering.
"""

from collections.abc import Callable, Sequence

# Longest overlap looked for: the configured overlap plus slack for the whitespace trimmed at chunk edges.
MAX_OVERLAP = 400
MIN_OVERLAP = 20


def trim_overlap(previous: str, text: str) -> str:
    """``text`` without its start if that repeats the end of ``previous`` (the chunker's overlap)."""
    limit = min(len(previous), len(text), MAX_OVERLAP)
    for size in range(limit, MIN_OVERLAP - 1, -1):
        if previous.endswith(text[:size]):
            return text[size:].lstrip()
    return text


def load_chunks(
    source_path: str, workspace_ids: set[str], scroll: Callable[[str, set[str]], list[dict]]
) -> list[dict]:
    """Chunks of one document in reading order, overlap removed, one dict per chunk.

    ``scroll`` is ``qdrant_store.scroll_document``; passing it in keeps this module free of the vector client.
    """
    chunks: list[dict] = []
    previous = ""
    previous_place: tuple | None = None
    for payload in scroll(source_path, workspace_ids):
        place = (payload.get("page"), payload.get("chapter"), payload.get("locator"))
        raw = payload.get("text") or ""
        # Overlap only exists inside one page or chapter; a new place starts clean.
        text = trim_overlap(previous, raw) if place == previous_place else raw
        previous, previous_place = raw, place
        if not text.strip():
            continue
        chunks.append(
            {
                "chunk_id": payload.get("chunk_id"),
                "file_id": payload.get("file_id"),
                "file_name": payload.get("file_name"),
                "source_path": payload.get("source_path") or source_path,
                "title": payload.get("title"),
                "language": payload.get("language"),
                "position": payload.get("position"),
                "locator": payload.get("locator"),
                "page": payload.get("page"),
                "chapter": payload.get("chapter"),
                "ocr": bool(payload.get("ocr")),
                "ocr_confidence": payload.get("ocr_confidence"),
                "preview": payload.get("preview"),
                "text": text,
                # Not a retrieval hit: no score, so the weak-match rule of `citations` never applies.
                "score": None,
            }
        )
    return chunks


# Characters a numbered source line adds around the text ("[12] title (file) | place | ").
LINE_OVERHEAD = 160


MIN_PIECE = 200


def chunk_cost(chunk: dict) -> int:
    return len(chunk["text"]) + LINE_OVERHEAD + len(chunk.get("title") or "") + len(chunk.get("file_name") or "")


def _cut_point(head: str) -> int:
    """Where to cut ``head``: after the last sentence end, else the last space, else (one huge word) at its end."""
    sentence = max(head.rfind(mark) for mark in (". ", "! ", "? ", "\n"))
    if sentence >= len(head) // 2:
        return sentence + 1
    space = head.rfind(" ")
    return space if space > 0 else len(head)


def split_oversized(chunks: Sequence[dict], max_chars: int) -> list[dict]:
    """Chunks whose numbered line would not fit ``max_chars`` cut into pieces that do, at a sentence or word boundary.

    Only for a window budget smaller than the chunks of the index (a setting changed after indexing); with the
    defaults nothing is cut. A piece keeps the chunk's page, chapter and OCR data, and gets its own id
    (``<id>~2``) so a citation can name it; the text is never dropped or reordered.
    """
    out: list[dict] = []
    for chunk in chunks:
        if chunk_cost(chunk) <= max_chars:
            out.append(chunk)
            continue
        room = max(MIN_PIECE, max_chars - (chunk_cost(chunk) - len(chunk["text"])))
        text, number = chunk["text"], 0
        while text:
            cut = len(text) if len(text) <= room else _cut_point(text[:room])
            piece, text = text[:cut].strip(), text[cut:].lstrip()
            if not piece:
                continue
            number += 1
            out.append({**chunk, "chunk_id": chunk["chunk_id"] if number == 1 else f"{chunk['chunk_id']}~{number}",
                        "text": piece})
    return out


def make_windows(chunks: Sequence[dict], max_chars: int) -> list[list[dict]]:
    """Consecutive chunks grouped so each group's numbered context stays within ``max_chars``.

    A chunk that alone exceeds the budget gets a window of its own; run ``split_oversized`` first to keep the
    budget (the job worker does).
    """
    windows: list[list[dict]] = []
    current: list[dict] = []
    size = 0
    for chunk in chunks:
        cost = chunk_cost(chunk)
        if current and size + cost > max_chars:
            windows.append(current)
            current, size = [], 0
        current.append(chunk)
        size += cost
    if current:
        windows.append(current)
    return windows
