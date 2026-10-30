"""Chunk-level source metadata and structured citations.

Document layers: pages/chapters and OCR confidence piggyback on chunks; storage keeps them
in the Qdrant payload; the API answer carries structured citations, grounding and confidence.
"""

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import fitz
import pytest
from teacher_common import ocr

TEXT = "A magyar nyelv ragozó nyelv, amelyben a toldalékok sorrendje szigorú szabályokat követ. " * 3


def _load_real(module_name: str, relative: str):
    """conftest stubs some heavy modules for the service tests; these tests need the real ones."""
    stub = sys.modules.get(module_name)
    path = Path(__file__).resolve().parents[1] / "services" / "common" / relative
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        if stub is not None:
            sys.modules[module_name] = stub
    return module


documents = _load_real("teacher_common.documents", "teacher_common/documents.py")
qdrant_store = _load_real("teacher_common.qdrant_store", "teacher_common/qdrant_store.py")


@pytest.fixture(autouse=True)
def real_modules(monkeypatch):
    monkeypatch.setitem(sys.modules, "teacher_common.documents", documents)
    monkeypatch.setitem(sys.modules, "teacher_common.qdrant_store", qdrant_store)


def make_cfg(tmp_path: Path, **overrides):
    values = {
        "ocr_enabled": True,
        "ocr_dpi": 72,
        "ocr_language": "hun+eng",
        "ocr_min_chars": 50,
        "ocr_timeout_seconds": 0,
        "ocr_max_pages": 0,
        "ocr_cache_dir": str(tmp_path / "cache"),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def make_pdf(path: Path, pages: list[str | None]) -> None:
    """A page is a text page (str) or a scanned page (None: image only, no text layer)."""
    doc = fitz.open()
    for text in pages:
        page = doc.new_page()
        if text is not None:
            page.insert_textbox(fitz.Rect(40, 40, 550, 800), text, fontsize=11)
        else:
            src = fitz.open()
            src_page = src.new_page()
            src_page.insert_text((60, 100), "scan", fontsize=40)
            page.insert_image(page.rect, pixmap=src_page.get_pixmap(dpi=50))
    doc.save(path)
    doc.close()
    return path


# --- chunk metadata on segments ------------------------------------------------


def test_chunk_segments_carry_page_chapter_and_ocr_fields():
    segments = [
        documents.TextSegment(text="szöveg " * 60, locator="oldal 3", page=3),
        documents.TextSegment(text="szöveg " * 60, locator="fejezet 2", chapter="2"),
        documents.TextSegment(text="szöveg " * 60, locator="oldal 4 (OCR)", page=4, ocr=True, ocr_confidence=42.0),
    ]
    chunks = documents.chunk_segments(segments, chunk_size=200, chunk_overlap=20)
    assert {c.page for c in chunks} == {3, None, 4}
    assert {c.chapter for c in chunks} == {None, "2"}
    ocr_chunks = [c for c in chunks if c.ocr]
    assert ocr_chunks and all(c.ocr_confidence == 42.0 for c in ocr_chunks)
    assert all(c.locator in {"oldal 3", "fejezet 2", "oldal 4 (OCR)"} for c in chunks)


def test_parse_pdf_sets_page_and_ocr_confidence(tmp_path, monkeypatch):
    def engine(image, lang, timeout):
        return ("Szkennelt oldal szövege. " * 6, 41.5)

    monkeypatch.setattr(ocr, "recognise", engine)
    pdf = make_pdf(tmp_path / "mixed.pdf", [TEXT, None])
    parsed = documents.parse_pdf(pdf, cfg=make_cfg(tmp_path))
    text_page, scan_page = parsed.segments[0], parsed.segments[1]
    assert (text_page.page, text_page.ocr, text_page.ocr_confidence) == (1, False, None)
    assert (scan_page.page, scan_page.ocr, scan_page.ocr_confidence) == (2, True, 41.5)
    assert scan_page.locator == "oldal 2 (OCR)"


def test_legacy_cache_without_confidence_keeps_pages_and_stays_unmeasured(tmp_path, monkeypatch):
    """Old cache files hold plain text: they keep loading, and the confidence stays unknown."""

    monkeypatch.setattr(ocr, "recognise", lambda image, lang, timeout: ("Szkennelt oldal szövege. " * 8, 88.0))
    pdf = make_pdf(tmp_path / "scan.pdf", [None])
    cfg = make_cfg(tmp_path)
    documents.parse_pdf(pdf, file_hash="ab" * 32, cfg=cfg)
    cache_file = next((tmp_path / "cache").rglob("*.json"))
    cache_file.write_text(json.dumps({"1": "Szkennelt oldal szövege."}), encoding="utf-8")

    def refuse(image, lang, timeout):
        raise AssertionError("cached pages must not call the engine")

    monkeypatch.setattr(ocr, "recognise", refuse)
    parsed = documents.parse_pdf(pdf, file_hash="ab" * 32, cfg=cfg)
    # The original OCR result differs from the legacy cache entry on purpose, so a cache hit is visible.
    assert [s.text for s in parsed.segments] == ["Szkennelt oldal szövege."]
    assert (parsed.segments[0].ocr, parsed.segments[0].ocr_confidence) == (True, None)


def test_cached_page_keeps_its_confidence(tmp_path, monkeypatch):
    calls = []

    def engine(image, lang, timeout):
        calls.append(lang)
        return ("Szkennelt oldal szövege. " * 6, 88.0)

    monkeypatch.setattr(ocr, "recognise", engine)
    pdf = make_pdf(tmp_path / "scan.pdf", [None])
    cfg = make_cfg(tmp_path)
    documents.parse_pdf(pdf, file_hash="cd" * 32, cfg=cfg)
    assert calls == ["hun+eng"]
    again = documents.parse_pdf(pdf, file_hash="cd" * 32, cfg=cfg)
    assert again.segments[0].ocr_confidence == 88.0


# --- tesseract data -> text + confidence ----------------------------------------


def test_mean_confidence_from_tesseract_data():
    data = {
        "block_num": [1, 1, 1],
        "par_num": [1, 1, 1],
        "line_num": [1, 1, 1],
        "word_num": [1, 2, 3],
        "text": ["két", "jó", "szó"],
        "conf": ["95", "88", "61"],
    }
    text, conf = ocr.confidence_from_data(data)
    assert conf == 81.3  # (95 + 88 + 61) / 3
    assert "két jó szó" in text


def test_confidence_from_data_skips_unscored_gaps():
    data = {
        "block_num": [1, 1],
        "par_num": [1, 1],
        "line_num": [1, 1],
        "word_num": [1, 2],
        "text": ["jó", "szó"],
        "conf": ["91", "-1"],
    }
    text, conf = ocr.confidence_from_data(data)
    assert text == "jó szó"
    assert conf == 91.0


# --- storage: the Qdrant payload carries the fields -----------------------------


def chunk_row(text: str, **fields):
    return {
        "chunk_id": "h:1",
        "point_id": "p1",
        "vector": [0.0],
        "position": 1,
        "locator": "oldal 3",
        "text": text,
        "preview": text,
        **fields,
    }


def test_build_points_payload_keeps_citation_fields():
    points = qdrant_store.build_points(
        source_path="/library/segédlet.pdf",
        title="Segédlet",
        author=None,
        language="hu",
        file_id="ab" * 32,
        chunk_rows=[
            chunk_row(
                "t",
                file_id="ab" * 32,
                file_name="segédlet.pdf",
                page=3,
                chapter=None,
                ocr=False,
                ocr_confidence=None,
            ),
        ],
    )
    payload = points[0].payload
    assert (payload["file_id"], payload["file_name"], payload["page"], payload["chapter"]) == (
        "ab" * 32,
        "segédlet.pdf",
        3,
        None,
    )
    assert (payload["ocr"], payload["ocr_confidence"]) == (False, None)


def test_build_points_names_the_file_when_the_row_has_no_name():
    points = qdrant_store.build_points(
        source_path="/library/konyv.pdf",
        title=None,
        author=None,
        language=None,
        file_id="cd" * 32,
        chunk_rows=[chunk_row("t", file_id="cd" * 32, page=7, chapter="3", ocr=True, ocr_confidence=55.0)],
    )
    payload = points[0].payload
    assert payload["file_name"] == "konyv.pdf"
    assert (payload["page"], payload["chapter"], payload["ocr"], payload["ocr_confidence"]) == (7, "3", True, 55.0)
