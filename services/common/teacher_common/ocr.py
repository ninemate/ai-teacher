"""OCR for PDF pages that have no usable text layer.

Pages are rendered with PyMuPDF and read with Tesseract. Results are cached on local disk, keyed by the
file hash, language and DPI, so the (read-only) library share is never written to and a file is never
OCR'd twice.
"""

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

import fitz

logger = logging.getLogger(__name__)


class OcrUnavailableError(RuntimeError):
    """The OCR engine or a requested language pack is not installed."""


class OcrTimeoutError(RuntimeError):
    """A single page exceeded the time that was left for the file."""


TIME_LIMIT_PREFIX = "OCR time limit"


def resumable(status: str | None, message: str | None) -> bool:
    """True when a later run would OCR more pages of the file: it stopped on the time budget, and cached
    pages cost no budget. A page limit or a failing page would stop at the same place again."""
    return status == "partial" and message is not None and message.startswith(TIME_LIMIT_PREFIX)


@dataclass
class OcrReport:
    pages_total: int = 0
    pages_ocr: int = 0
    pages_cached: int = 0
    pages_pending: int = 0
    status: str = "complete"
    message: str | None = None
    page_numbers: list[int] = field(default_factory=list)

    @property
    def used(self) -> bool:
        return self.pages_ocr > 0


def needs_ocr(text: str, min_chars: int) -> bool:
    """True when a page's text layer is missing or too thin to be real content."""
    return len(text.strip()) < min_chars


def confidence_from_data(data) -> tuple[str, float | None]:
    """Rebuild the page text from a Tesseract TSV dict and score it.

    The mean word confidence (0-100, unscored gaps excluded) tells how sure the engine was;
    ``None`` when no word was scored, e.g. an empty page.
    """
    words: list[tuple[tuple[int, int, int, int], str, int]] = []
    rows = zip(data["block_num"], data["par_num"], data["line_num"], data["word_num"], data["text"], data["conf"])
    for block, par, line, word, text, conf in rows:
        if isinstance(text, str) and text.strip():
            words.append(((int(block), int(par), int(line), int(word)), text.strip(), int(conf)))
    lines: list[str] = []
    current: list[str] = []
    key: tuple[int, int, int, int] | None = None
    for word_key, text, _conf in words:
        if key is not None and word_key[:3] != key[:3]:
            lines.append(" ".join(current))
            current = []
        key = word_key
        current.append(text)
    if current:
        lines.append(" ".join(current))
    from teacher_common.documents import normalize_text

    text = normalize_text("\n".join(lines))
    scored = [conf for _, _, conf in words if conf >= 0]
    confidence = round(sum(scored) / len(scored), 1) if scored else None
    return text, confidence


def recognise(image, lang: str, timeout: float) -> tuple[str, float | None]:
    """Run Tesseract on a PIL image once and get both text and confidence from it.

    Isolated so tests can replace the engine.
    """
    import pytesseract

    try:
        data = pytesseract.image_to_data(image, lang=lang, timeout=timeout, output_type=pytesseract.Output.DICT)
    except pytesseract.TesseractNotFoundError as exc:
        raise OcrUnavailableError("tesseract binary not found") from exc
    except RuntimeError as exc:
        if "timeout" in str(exc).lower():
            raise OcrTimeoutError(str(exc)) from exc
        raise
    return confidence_from_data(data)


def render_page(page: fitz.Page, dpi: int):
    from PIL import Image

    pix = page.get_pixmap(dpi=dpi, colorspace=fitz.csRGB, alpha=False)
    return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)


class OcrCache:
    """Per-document JSON file of page number -> {text, conf}, stored outside the library share.

    Files written by an older format (a plain text string per page) load without the confidence.
    """

    def __init__(self, cache_dir: Path, file_hash: str, lang: str, dpi: int):
        safe_lang = lang.replace("+", "-")
        self.path = cache_dir / file_hash[:2] / f"{file_hash}.{safe_lang}.{dpi}.json"
        self.pages: dict[int, dict] = {}
        self._dirty = False
        if self.path.exists():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                self.pages = {
                    int(k): v if isinstance(v, dict) else {"text": v, "conf": None} for k, v in raw.items()
                }
            except (OSError, ValueError):
                logger.warning("Ignoring unreadable OCR cache %s", self.path)

    def put(self, page_number: int, text: str, confidence: float | None) -> None:
        self.pages[page_number] = {"text": text, "conf": confidence}
        self._dirty = True

    def save(self) -> None:
        if not self._dirty:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.pages, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)
        self._dirty = False


class PdfOcr:
    """OCR budget for one PDF: shared deadline, page limit and cache."""

    def __init__(self, cfg, file_hash: str):
        self.cfg = cfg
        self.cache = OcrCache(Path(cfg.ocr_cache_dir), file_hash, cfg.ocr_language, cfg.ocr_dpi)
        self.report = OcrReport()
        self._deadline = time.monotonic() + cfg.ocr_timeout_seconds if cfg.ocr_timeout_seconds > 0 else None
        self._stopped = False
        self._attempted = 0

    def read_page(self, page: fitz.Page, page_number: int) -> tuple[str, float | None]:
        """(OCR text for the page, mean word confidence), or ("", None) when it is cached-empty,
        over budget or failed."""
        report = self.report
        if page_number in self.cache.pages:
            self._attempted += 1
            entry = self.cache.pages[page_number]
            text = entry.get("text", "")
            if text:
                report.pages_cached += 1
                report.pages_ocr += 1
                report.page_numbers.append(page_number)
            return text, entry.get("conf")
        if self._stopped:
            report.pages_pending += 1
            return "", None
        if self.cfg.ocr_max_pages > 0 and self._attempted >= self.cfg.ocr_max_pages:
            self._stop("partial", f"OCR page limit of {self.cfg.ocr_max_pages} reached")
            report.pages_pending += 1
            return "", None
        remaining = None if self._deadline is None else self._deadline - time.monotonic()
        if remaining is not None and remaining <= 0:
            self._stop("partial", f"{TIME_LIMIT_PREFIX} of {self.cfg.ocr_timeout_seconds}s reached")
            report.pages_pending += 1
            return "", None
        self._attempted += 1
        try:
            text, confidence = self._recognise(page, remaining)
        except OcrTimeoutError:
            self._stop("partial", f"{TIME_LIMIT_PREFIX} of {self.cfg.ocr_timeout_seconds}s reached")
            report.pages_pending += 1
            return "", None
        except OcrUnavailableError as exc:
            self._stop("unavailable", str(exc))
            report.pages_pending += 1
            return "", None
        except Exception as exc:  # noqa: BLE001 - a bad page must not lose the rest of the document
            logger.warning("OCR failed on page %s: %s", page_number, exc)
            report.pages_pending += 1
            report.status = "partial"
            report.message = f"OCR failed on page {page_number}: {exc}"
            return "", None
        self.cache.put(page_number, text, confidence)
        # Written after every page, so a run that is stopped mid-file (deploy, restart) keeps its work.
        self._save_cache()
        if text:
            report.pages_ocr += 1
            report.page_numbers.append(page_number)
        return text, confidence

    def _recognise(self, page: fitz.Page, remaining: float | None) -> tuple[str, float | None]:
        timeout = int(max(1, remaining)) if remaining is not None else 0
        return recognise(render_page(page, self.cfg.ocr_dpi), self.cfg.ocr_language, timeout)

    def _stop(self, status: str, message: str) -> None:
        self._stopped = True
        self.report.status = status
        self.report.message = message

    def _save_cache(self) -> None:
        try:
            self.cache.save()
        except OSError as exc:
            logger.warning("Could not write OCR cache %s: %s", self.cache.path, exc)

    def finish(self) -> OcrReport:
        self._save_cache()
        if self.report.pages_pending == 0 and self.report.status == "complete":
            self.report.message = None
        return self.report


def record_ocr(session, source_path: str, file_hash: str, report: OcrReport, cfg) -> None:
    """Upsert the SQLite marker that a document was OCR'd (and how completely)."""
    from sqlalchemy import select

    from teacher_common.models import OcrRecord

    row = session.execute(select(OcrRecord).where(OcrRecord.source_path == source_path)).scalars().first()
    message = report.message
    if (
        row is not None
        and row.source_hash == file_hash
        and resumable(row.status, row.message)
        and resumable(report.status, message)
        and report.pages_pending >= row.pages_pending
    ):
        # Not one more page fitted into the budget (a single page slower than OCR_TIMEOUT_SECONDS):
        # stop resuming instead of re-reading the file on every run.
        message = f"{message}; no progress since the last run, raise OCR_TIMEOUT_SECONDS and run a full reindex"
        message = message.replace(TIME_LIMIT_PREFIX, "Stalled: " + TIME_LIMIT_PREFIX.lower(), 1)
    if row is None:
        row = OcrRecord(source_path=source_path)
        session.add(row)
    row.source_hash = file_hash
    row.language = cfg.ocr_language
    row.dpi = cfg.ocr_dpi
    row.pages_total = report.pages_total
    row.pages_ocr = report.pages_ocr
    row.pages_pending = report.pages_pending
    row.status = report.status
    row.message = message
