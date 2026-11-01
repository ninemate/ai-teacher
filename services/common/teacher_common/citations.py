"""Structured citations for RAG answers: which file, page or chapter an answer came from, and how sure it is.

Pure functions only (no database, no HTTP). The model is shown numbered sources and asked to put ``[n]``
after every statement taken from source n and ``[forrás nélkül]`` after statements of its own; ``assess``
reads those markers back.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

UNSOURCED_MARKER = "[forrás nélkül]"

# Appended to the task instruction, so the rule holds even with a custom TEACHER_PROMPT_PATH.
CITATION_RULES = (
    "Hivatkozási szabályok:\n"
    "- Minden állítás után, amely egy forrásrészletből származik, írd oda a forrás számát szögletes zárójelben, "
    "pl. [1] vagy [1, 3].\n"
    f"- Ha valamit nem a forrásrészletek alapján mondasz (saját tudás, példa, következtetés), jelöld így: "
    f"{UNSOURCED_MARKER}.\n"
    "- Ne hivatkozz olyan számra, amely nincs a forrásrészletek között.\n"
    "- Ha egy felhasznált forrásrészlet BIZONYTALAN jelölésű, mondd ki a válaszban, hogy az a forrás bizonytalan "
    "(pl. hibás szkennelés vagy gyenge egyezés)."
)

_CITE = re.compile(r"\[(\d+(?:\s*[,;]\s*\d+)*)\]")
_UNSOURCED = re.compile(r"\[\s*forrás\s+nélkül\s*\]", re.IGNORECASE)

REASON_TEXT = {
    "weak_match": "gyenge egyezés",
    "ocr_low_confidence": "OCR, {conf}% bizonyosság",
    "ocr_unmeasured": "OCR, ismeretlen bizonyosság",
}


@dataclass(frozen=True)
class Limits:
    # Retrieval score (cosine similarity) below which a match is weak; depends on the embedding model.
    weak_score: float
    # Mean Tesseract word confidence (0-100) below which OCR'd text is not trusted.
    ocr_min_confidence: float


def _place(page, chapter, locator: str | None) -> str | None:
    if page is not None:
        return f"{page}. oldal"
    if chapter:
        return f"{chapter}. fejezet" if str(chapter).isdigit() else f"{chapter} fejezet"
    return locator or None


def uncertain_reasons(hit: dict, limits: Limits) -> list[str]:
    reasons: list[str] = []
    score = hit.get("score")
    if score is not None and score < limits.weak_score:
        reasons.append("weak_match")
    if hit.get("ocr"):
        confidence = hit.get("ocr_confidence")
        if confidence is None:
            reasons.append("ocr_unmeasured")
        elif confidence < limits.ocr_min_confidence:
            reasons.append("ocr_low_confidence")
    return reasons


def build_citations(hits: Sequence[dict], limits: Limits) -> list[dict]:
    """One citation per retrieved chunk, numbered from 1 in retrieval order."""
    citations: list[dict] = []
    for number, hit in enumerate(hits, start=1):
        source_path = hit.get("source_path") or ""
        file_name = hit.get("file_name") or Path(source_path).name or None
        place = _place(hit.get("page"), hit.get("chapter"), hit.get("locator"))
        reasons = uncertain_reasons(hit, limits)
        citations.append(
            {
                "id": number,
                "chunk_id": hit.get("chunk_id"),
                "file_id": hit.get("file_id"),
                "file_name": file_name,
                "source_path": source_path or None,
                "title": hit.get("title"),
                "page": hit.get("page"),
                "chapter": hit.get("chapter"),
                "locator": hit.get("locator"),
                "label": ", ".join(part for part in (file_name or "ismeretlen dokumentum", place) if part),
                "score": hit.get("score"),
                "ocr": bool(hit.get("ocr")),
                "ocr_confidence": hit.get("ocr_confidence"),
                "uncertain": bool(reasons),
                "uncertain_reasons": reasons,
                "cited": False,
                "preview": hit.get("preview"),
            }
        )
    return citations


def _warning(citation: dict) -> str:
    conf = citation["ocr_confidence"]
    texts = [REASON_TEXT[r].format(conf=round(conf) if conf is not None else "?") for r in citation["uncertain_reasons"]]
    return f"BIZONYTALAN: {', '.join(texts)} | " if texts else ""


def build_context(citations: Sequence[dict], hits: Sequence[dict], max_chars: int) -> tuple[str, list[dict]]:
    """Numbered context block and the citations whose whole text fit in ``max_chars``."""
    lines: list[str] = []
    used: list[dict] = []
    total = 0
    for citation, hit in zip(citations, hits, strict=True):
        title = citation["title"] or citation["file_name"] or "ismeretlen dokumentum"
        name = f" ({citation['file_name']})" if citation["file_name"] and citation["file_name"] != title else ""
        place = _place(citation["page"], citation["chapter"], citation["locator"]) or "ismeretlen hely"
        line = f"[{citation['id']}] {title}{name} | {place} | {_warning(citation)}{hit.get('text') or ''}"
        if used and total + len(line) > max_chars:
            break
        lines.append(line if used else line[:max_chars])
        total += len(line)
        used.append(citation)
    return "\n".join(lines), used


def cited_numbers(answer: str, count: int) -> list[int]:
    """Source numbers the answer refers to, in order of first use; invented numbers are dropped."""
    numbers: dict[int, None] = {}
    for match in _CITE.finditer(answer):
        for part in re.split(r"[,;]", match.group(1)):
            number = int(part)
            if 1 <= number <= count:
                numbers[number] = None
    return list(numbers)


def assess(answer: str, citations: list[dict]) -> dict:
    """How the answer is grounded. Marks the cited citations in place.

    status: "grounded" (every marked statement has a source), "partial" (some statements are marked as the
    model's own), "no_source" (nothing retrieved, or the answer cites none of the sources).
    """
    cited = cited_numbers(answer, len(citations))
    for citation in citations:
        citation["cited"] = citation["id"] in cited
    unsourced = bool(_UNSOURCED.search(answer))
    if not cited:
        status = "no_source"
    elif unsourced:
        status = "partial"
    else:
        status = "grounded"
    relevant = [c for c in citations if c["cited"]] or citations
    low_confidence = bool(relevant) and (
        any(c["uncertain"] for c in relevant) if cited else all(c["uncertain"] for c in relevant)
    )

    notices: list[str] = []
    if not citations:
        notices.append("A könyvtárban nem találtam forrást ehhez a kérdéshez; a válasz nem dokumentumokon alapul.")
    elif not cited:
        notices.append("A válasz nem hivatkozik egyik forrásra sem; kezeld a modell saját magyarázataként.")
    elif unsourced:
        notices.append(f"A {UNSOURCED_MARKER} jelölésű részek a modell saját magyarázatai, nem a dokumentumokból valók.")
    if low_confidence:
        notices.append(
            "A felhasznált források egy része bizonytalan (gyenge egyezés vagy rossz minőségű szkennelés); "
            "ellenőrizd az eredeti dokumentumban."
        )
    return {
        "status": status,
        "cited": cited,
        "unsourced_statements": unsourced,
        "low_confidence": low_confidence,
        "notice": " ".join(notices) or None,
    }
