import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional

import fitz
from bs4 import BeautifulSoup
from docx import Document as DocxDocument
from ebooklib import ITEM_DOCUMENT, epub
from langdetect import detect


SUPPORTED_EXTENSIONS = {".pdf", ".epub", ".txt", ".md", ".docx"}
UNSUPPORTED_BUT_KNOWN = {".mobi", ".azw", ".azw3"}


@dataclass
class TextSegment:
    text: str
    locator: str


@dataclass
class ParsedDocument:
    title: Optional[str]
    author: Optional[str]
    language: Optional[str]
    segments: List[TextSegment]


def hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def normalize_text(text: str) -> str:
    text = text.replace("\x00", " ")
    text = re.sub(r"\r\n?", "\n", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def detect_language(text: str) -> Optional[str]:
    sample = text[:2000].strip()
    if len(sample) < 50:
        return None
    try:
        return detect(sample)
    except Exception:
        return None


def parse_document(path: Path) -> ParsedDocument:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return parse_pdf(path)
    if suffix == ".epub":
        return parse_epub(path)
    if suffix in {".txt", ".md"}:
        return parse_plaintext(path)
    if suffix == ".docx":
        return parse_docx(path)
    if suffix in UNSUPPORTED_BUT_KNOWN:
        raise ValueError(
            f"{suffix} is not enabled in the MVP parser. Convert to EPUB/PDF/TXT or add Calibre-based conversion later."
        )
    raise ValueError(f"Unsupported file type: {suffix}")


def parse_pdf(path: Path) -> ParsedDocument:
    segments: List[TextSegment] = []
    with fitz.open(path) as pdf:
        metadata = pdf.metadata or {}
        for index, page in enumerate(pdf, start=1):
            text = normalize_text(page.get_text("text"))
            if text:
                segments.append(TextSegment(text=text, locator=f"oldal {index}"))
    combined = "\n\n".join(segment.text for segment in segments)
    return ParsedDocument(
        title=metadata.get("title") or path.stem,
        author=metadata.get("author"),
        language=detect_language(combined),
        segments=segments,
    )


def parse_epub(path: Path) -> ParsedDocument:
    book = epub.read_epub(str(path))
    segments: List[TextSegment] = []
    title = _first_metadata(book, "DC", "title") or path.stem
    author = _first_metadata(book, "DC", "creator")
    for item in book.get_items_of_type(ITEM_DOCUMENT):
        soup = BeautifulSoup(item.get_body_content(), "html.parser")
        text = normalize_text(soup.get_text("\n"))
        if text:
            label = item.get_name() or "szakasz"
            segments.append(TextSegment(text=text, locator=f"fejezet {label}"))
    combined = "\n\n".join(segment.text for segment in segments)
    return ParsedDocument(
        title=title,
        author=author,
        language=detect_language(combined),
        segments=segments,
    )


def parse_plaintext(path: Path) -> ParsedDocument:
    text = normalize_text(path.read_text(encoding="utf-8", errors="ignore"))
    return ParsedDocument(
        title=path.stem,
        author=None,
        language=detect_language(text),
        segments=[TextSegment(text=text, locator="szövegtörzs")] if text else [],
    )


def parse_docx(path: Path) -> ParsedDocument:
    doc = DocxDocument(path)
    parts = [paragraph.text.strip() for paragraph in doc.paragraphs if paragraph.text.strip()]
    text = normalize_text("\n\n".join(parts))
    return ParsedDocument(
        title=path.stem,
        author=None,
        language=detect_language(text),
        segments=[TextSegment(text=text, locator="dokumentum")] if text else [],
    )


def chunk_segments(segments: Iterable[TextSegment], chunk_size: int, chunk_overlap: int) -> List[TextSegment]:
    chunks: List[TextSegment] = []
    for segment in segments:
        text = normalize_text(segment.text)
        if not text:
            continue
        start = 0
        while start < len(text):
            end = min(len(text), start + chunk_size)
            if end < len(text):
                split = text.rfind(" ", start, end)
                if split > start + 100:
                    end = split
            chunk = text[start:end].strip()
            if chunk:
                chunks.append(TextSegment(text=chunk, locator=segment.locator))
            if end >= len(text):
                break
            start = max(0, end - chunk_overlap)
    return chunks


def iter_library_files(root: Path, extensions: Iterable[str]) -> Iterable[Path]:
    allowed = {item.lower() for item in extensions}
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.suffix.lower() in allowed.union(UNSUPPORTED_BUT_KNOWN):
            yield path


def _first_metadata(book, namespace: str, key: str) -> Optional[str]:
    values = book.get_metadata(namespace, key)
    if not values:
        return None
    return values[0][0]
