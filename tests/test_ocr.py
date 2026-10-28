from pathlib import Path
from types import SimpleNamespace

import fitz
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from teacher_common import documents, ocr
from teacher_common.db import Base
from teacher_common.models import OcrRecord

TEXT = "A magyar nyelv ragozó nyelv, amelyben a toldalékok sorrendje szigorú szabályokat követ. " * 3


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


def make_pdf(path: Path, pages: list[str | None]) -> Path:
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


@pytest.fixture
def fake_engine(monkeypatch):
    calls: list[tuple[str, float]] = []

    def fake(image, lang, timeout):
        calls.append((lang, timeout))
        return f"  Szkennelt oldal szövege {len(calls)}  \n"

    monkeypatch.setattr(ocr, "ocr_image", fake)
    return calls


def test_needs_ocr_threshold():
    assert ocr.needs_ocr("", 50)
    assert ocr.needs_ocr("  12  \n", 50)
    assert ocr.needs_ocr("x" * 49, 50)
    assert not ocr.needs_ocr("x" * 50, 50)


def test_text_pdf_never_touches_ocr(tmp_path, fake_engine):
    pdf = make_pdf(tmp_path / "text.pdf", [TEXT, TEXT])
    parsed = documents.parse_pdf(pdf, cfg=make_cfg(tmp_path))
    assert fake_engine == []
    assert parsed.ocr is None
    assert [s.locator for s in parsed.segments] == ["oldal 1", "oldal 2"]
    assert not (tmp_path / "cache").exists()


def test_scanned_pages_are_ocrd_and_mixed_pdf_keeps_text_layer(tmp_path, fake_engine):
    pdf = make_pdf(tmp_path / "mixed.pdf", [TEXT, None, None])
    parsed = documents.parse_pdf(pdf, file_hash="ab" * 32, cfg=make_cfg(tmp_path))
    assert [s.locator for s in parsed.segments] == ["oldal 1", "oldal 2 (OCR)", "oldal 3 (OCR)"]
    assert parsed.segments[1].text == "Szkennelt oldal szövege 1"
    assert fake_engine[0][0] == "hun+eng"
    assert parsed.ocr.pages_total == 3
    assert parsed.ocr.pages_ocr == 2
    assert parsed.ocr.status == "complete"


def test_ocr_disabled_skips_scanned_pages(tmp_path, fake_engine):
    pdf = make_pdf(tmp_path / "scan.pdf", [None])
    parsed = documents.parse_pdf(pdf, cfg=make_cfg(tmp_path, ocr_enabled=False))
    assert fake_engine == []
    assert parsed.segments == []
    assert parsed.ocr.status == "disabled"
    assert parsed.ocr.pages_pending == 1


def test_ocr_disabled_text_pdf_has_no_report(tmp_path):
    parsed = documents.parse_pdf(make_pdf(tmp_path / "t.pdf", [TEXT]), cfg=make_cfg(tmp_path, ocr_enabled=False))
    assert parsed.ocr is None


def test_second_run_uses_cache_not_engine(tmp_path, fake_engine):
    pdf = make_pdf(tmp_path / "scan.pdf", [None, None])
    cfg = make_cfg(tmp_path)
    documents.parse_pdf(pdf, file_hash="cd" * 32, cfg=cfg)
    assert len(fake_engine) == 2
    again = documents.parse_pdf(pdf, file_hash="cd" * 32, cfg=cfg)
    assert len(fake_engine) == 2
    assert again.ocr.pages_cached == 2
    assert [s.text for s in again.segments] == ["Szkennelt oldal szövege 1", "Szkennelt oldal szövege 2"]


def test_cache_is_keyed_by_language_and_dpi(tmp_path, fake_engine):
    pdf = make_pdf(tmp_path / "scan.pdf", [None])
    documents.parse_pdf(pdf, file_hash="ef" * 32, cfg=make_cfg(tmp_path))
    documents.parse_pdf(pdf, file_hash="ef" * 32, cfg=make_cfg(tmp_path, ocr_language="eng"))
    assert len(fake_engine) == 2


def test_source_file_is_not_modified(tmp_path, fake_engine):
    pdf = make_pdf(tmp_path / "scan.pdf", [None])
    before = (pdf.stat().st_mtime_ns, pdf.read_bytes())
    documents.parse_pdf(pdf, cfg=make_cfg(tmp_path))
    assert (pdf.stat().st_mtime_ns, pdf.read_bytes()) == before
    assert [p.name for p in tmp_path.iterdir() if p.is_file()] == ["scan.pdf"]


def test_max_pages_limit_leaves_rest_pending(tmp_path, fake_engine):
    pdf = make_pdf(tmp_path / "scan.pdf", [None, None, None])
    parsed = documents.parse_pdf(pdf, cfg=make_cfg(tmp_path, ocr_max_pages=2))
    assert len(fake_engine) == 2
    assert len(parsed.segments) == 2
    assert parsed.ocr.status == "partial"
    assert parsed.ocr.pages_pending == 1


def test_timeout_stops_ocr_but_keeps_finished_pages(tmp_path, monkeypatch):
    calls = []

    def slow(image, lang, timeout):
        calls.append(timeout)
        if len(calls) == 2:
            raise ocr.OcrTimeoutError("Tesseract process timeout")
        return "első oldal szövege"

    monkeypatch.setattr(ocr, "ocr_image", slow)
    pdf = make_pdf(tmp_path / "scan.pdf", [None, None, None])
    cfg = make_cfg(tmp_path, ocr_timeout_seconds=60)
    parsed = documents.parse_pdf(pdf, file_hash="12" * 32, cfg=cfg)
    assert len(calls) == 2  # third page is not attempted after the timeout
    assert [s.text for s in parsed.segments] == ["első oldal szövege"]
    assert parsed.ocr.status == "partial"
    assert parsed.ocr.pages_pending == 2
    assert calls[0] <= 60


def test_max_pages_counts_blank_pages(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(ocr, "ocr_image", lambda i, lang, t: calls.append(1) or "")
    pdf = make_pdf(tmp_path / "blank.pdf", [None] * 5)
    parsed = documents.parse_pdf(pdf, cfg=make_cfg(tmp_path, ocr_max_pages=2))
    assert len(calls) == 2
    assert parsed.ocr.pages_pending == 3


def test_page_failure_does_not_lose_other_pages(tmp_path, monkeypatch):
    calls = []

    def flaky(image, lang, timeout):
        calls.append(1)
        if len(calls) == 1:
            raise ValueError("bad image")
        return "jó oldal szövege"

    monkeypatch.setattr(ocr, "ocr_image", flaky)
    pdf = make_pdf(tmp_path / "scan.pdf", [None, None])
    parsed = documents.parse_pdf(pdf, cfg=make_cfg(tmp_path))
    assert [s.locator for s in parsed.segments] == ["oldal 2 (OCR)"]
    assert parsed.ocr.status == "partial"
    assert "page 1" in parsed.ocr.message


def test_missing_engine_is_reported_not_raised(tmp_path, monkeypatch):
    def missing(image, lang, timeout):
        raise ocr.OcrUnavailableError("tesseract binary not found")

    monkeypatch.setattr(ocr, "ocr_image", missing)
    pdf = make_pdf(tmp_path / "mixed.pdf", [TEXT, None])
    parsed = documents.parse_pdf(pdf, cfg=make_cfg(tmp_path))
    assert [s.locator for s in parsed.segments] == ["oldal 1"]
    assert parsed.ocr.status == "unavailable"


def test_record_ocr_upserts_sqlite_marker(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 't.db'}")
    Base.metadata.create_all(engine)
    cfg = make_cfg(tmp_path)
    with sessionmaker(bind=engine)() as session:
        ocr.record_ocr(session, "/library/a.pdf", "h1", ocr.OcrReport(pages_total=4, pages_ocr=3, pages_pending=1, status="partial"), cfg)
        session.commit()
        ocr.record_ocr(session, "/library/a.pdf", "h2", ocr.OcrReport(pages_total=4, pages_ocr=4), cfg)
        session.commit()
        rows = session.execute(select(OcrRecord)).scalars().all()
        assert len(rows) == 1
        assert (rows[0].source_hash, rows[0].pages_ocr, rows[0].status, rows[0].language) == ("h2", 4, "complete", "hun+eng")


def test_pytesseract_timeout_is_translated(monkeypatch):
    import pytesseract

    def boom(*a, **k):
        raise RuntimeError("Tesseract process timeout")

    monkeypatch.setattr(pytesseract, "image_to_string", boom)
    with pytest.raises(ocr.OcrTimeoutError):
        ocr.ocr_image(object(), "eng", 5)


def _tesseract_has(lang: str) -> bool:
    try:
        import pytesseract

        return lang in pytesseract.get_languages()
    except Exception:  # noqa: BLE001 - any failure means the engine is not usable here
        return False


@pytest.mark.skipif(not (_tesseract_has("hun") and _tesseract_has("eng")), reason="tesseract hun+eng not installed")
def test_real_tesseract_reads_hungarian_scan(tmp_path):
    src = fitz.open()
    page = src.new_page()
    page.insert_textbox(fitz.Rect(40, 40, 550, 300), "Árvíztűrő tükörfúrógép és egészséges magyar szöveg", fontsize=24)
    scan = fitz.open()
    sp = scan.new_page()
    sp.insert_image(sp.rect, pixmap=page.get_pixmap(dpi=150))
    pdf = tmp_path / "scan.pdf"
    scan.save(pdf)
    parsed = documents.parse_pdf(pdf, cfg=make_cfg(tmp_path, ocr_dpi=200))
    assert parsed.ocr.status == "complete"
    assert "magyar" in parsed.segments[0].text.lower()
