"""Rebuild a document's text in reading order from the stored chunks, and cut it into sections and segments.

Ingestion keeps every chunk (full text, 1-based ``position``, page or chapter locator, OCR confidence) in the
vector store, in the order it read them. Consecutive chunks of one page or chapter overlap by ``chunk_overlap``
characters, so reading order means dropping that repeated tail. Nothing else is needed from ingestion, so
documents indexed before this feature need no reindex.

Sections follow what the file gives: one per PDF page, one per ebook chapter, and for flat text (TXT, MD, DOCX)
markdown headings or, failing that, parts of about ``SECTION_TARGET_CHARS``. Segments are what is synthesized
and shown one at a time: paragraphs cut at sentence ends to at most ``max_chars``.
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass, field

SECTION_TARGET_CHARS = 6000
MIN_ANCHOR = 24  # shortest repeated run that is trusted to be overlap rather than coincidence
_HEADING = re.compile(r"^#{1,3}\s+\S")
_SENTENCE_END = re.compile(r"(?<=[.!?…])\s+(?=[\"'„”»(\[]?[\w])")
_HYPHEN_BREAK = re.compile(r"(?<=[a-záéíóöőúüű])-\n(?=[a-záéíóöőúüű])")


@dataclass
class Segment:
    text: str
    ocr: bool = False
    ocr_confidence: float | None = None
    low_confidence: bool = False


@dataclass
class Section:
    title: str
    locator: str
    page: int | None = None
    segments: list[Segment] = field(default_factory=list)

    @property
    def chars(self) -> int:
        return sum(len(segment.text) for segment in self.segments)


@dataclass
class _Piece:
    """Overlap-free text of one page or chapter, with the OCR facts of its chunks."""

    locator: str
    page: int | None
    chapter: str | None
    text: str
    ocr: bool
    confidences: list[float | None]


def join_without_overlap(previous: str, following: str, max_overlap: int) -> str:
    """Append ``following`` to ``previous``, leaving out the start of it that ``previous`` already ends with."""
    if not previous:
        return following
    window = previous[-(max_overlap + 64):]
    anchor = following[:MIN_ANCHOR] if len(following) >= MIN_ANCHOR else following
    start = window.find(anchor)
    while start != -1:
        tail = window[start:]
        if following.startswith(tail):
            return previous + following[len(tail):]
        start = window.find(anchor, start + 1)
    return previous + "\n" + following if not previous.endswith("\n") else previous + following


def build_pieces(chunks: Iterable[dict], max_overlap: int) -> list[_Piece]:
    """Group chunk payloads (any order) by position into per-locator pieces without the repeated overlap."""
    pieces: list[_Piece] = []
    for chunk in sorted(chunks, key=lambda item: item.get("position") or 0):
        text = (chunk.get("text") or "").strip()
        if not text:
            continue
        locator = chunk.get("locator") or ""
        last = pieces[-1] if pieces else None
        if last is None or last.locator != locator or last.page != chunk.get("page") or last.chapter != chunk.get("chapter"):
            last = _Piece(locator, chunk.get("page"), chunk.get("chapter"), "", bool(chunk.get("ocr")), [])
            pieces.append(last)
            last.text = text
        else:
            last.text = join_without_overlap(last.text, text, max_overlap)
        last.ocr = last.ocr or bool(chunk.get("ocr"))
        last.confidences.append(chunk.get("ocr_confidence") if chunk.get("ocr") else None)
    return pieces


def _paragraphs(text: str) -> list[str]:
    text = _HYPHEN_BREAK.sub("", text)
    result = []
    for block in re.split(r"\n\s*\n", text):
        # A single line break inside a paragraph is a PDF line wrap, not a pause.
        flat = re.sub(r"\s*\n\s*", " ", block).strip()
        if flat:
            result.append(flat)
    return result


def _cut(text: str, max_chars: int) -> list[str]:
    """Split one paragraph at sentence ends, and a sentence longer than the limit at spaces."""
    parts: list[str] = []
    current = ""
    for sentence in _SENTENCE_END.split(text):
        while len(sentence) > max_chars:
            split = sentence.rfind(" ", 0, max_chars)
            split = split if split > max_chars // 2 else max_chars
            head, sentence = sentence[:split].strip(), sentence[split:].strip()
            if current:
                parts.append(current)
                current = ""
            parts.append(head)
        if current and len(current) + 1 + len(sentence) > max_chars:
            parts.append(current)
            current = sentence
        else:
            current = f"{current} {sentence}".strip()
    if current:
        parts.append(current)
    return [part for part in parts if part]


def split_segments(text: str, max_chars: int) -> list[str]:
    segments: list[str] = []
    for paragraph in _paragraphs(text):
        segments.extend(_cut(paragraph, max_chars))
    return segments


def _heading_title(first_line: str) -> str | None:
    line = first_line.strip().lstrip("#").strip()
    if 0 < len(line) <= 80 and not line.endswith((".", ",", ";", ":")):
        return line
    return None


def _split_flat(text: str) -> list[tuple[str | None, str]]:
    """Cut a long flat text at markdown headings, or at paragraph ends near the target size."""
    lines = text.split("\n")
    if any(_HEADING.match(line) for line in lines):
        parts: list[tuple[str | None, list[str]]] = [(None, [])]
        for line in lines:
            if _HEADING.match(line):
                parts.append((line.lstrip("#").strip(), [line]))
            else:
                parts[-1][1].append(line)
        return [(title, "\n".join(body)) for title, body in parts if "".join(body).strip()]
    if len(text) <= SECTION_TARGET_CHARS * 1.5:
        return [(None, text)]
    result: list[tuple[str | None, str]] = []
    current: list[str] = []
    size = 0
    for paragraph in re.split(r"\n\s*\n", text):
        current.append(paragraph)
        size += len(paragraph)
        if size >= SECTION_TARGET_CHARS:
            result.append((None, "\n\n".join(current)))
            current, size = [], 0
    if current:
        result.append((None, "\n\n".join(current)))
    return result


def build_sections(chunks: Iterable[dict], *, max_overlap: int, max_chars: int, ocr_min_confidence: float) -> list[Section]:
    """The document as sections of segments, with OCR text under ``ocr_min_confidence`` flagged."""
    sections: list[Section] = []
    for piece in build_pieces(chunks, max_overlap):
        known = [value for value in piece.confidences if value is not None]
        confidence = sum(known) / len(known) if known else None
        # Same rule as the source citations: OCR text with an unmeasured or low confidence is not trusted.
        low = piece.ocr and (confidence is None or confidence < ocr_min_confidence)
        flat = piece.page is None and piece.chapter is None
        parts = _split_flat(piece.text) if flat else [(None, piece.text)]
        for number, (heading, body) in enumerate(parts, start=1):
            texts = split_segments(body, max_chars)
            if not texts:
                continue
            title = heading or _label(piece, body, number, len(parts))
            section = Section(title=title, locator=piece.locator, page=piece.page)
            section.segments = [
                Segment(text=text, ocr=piece.ocr, ocr_confidence=confidence if piece.ocr else None, low_confidence=low)
                for text in texts
            ]
            sections.append(section)
    return sections


def _label(piece: _Piece, body: str, number: int, count: int) -> str:
    if piece.page is not None:
        return f"{piece.page}. oldal"
    if piece.chapter is not None:
        heading = _heading_title(body.strip().split("\n", 1)[0])
        return heading or f"{piece.locator}".replace("fejezet ", "").strip() or "Fejezet"
    return f"{number}. rész" if count > 1 else "Teljes szöveg"
