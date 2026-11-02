"""Reading-order text, sections and segments rebuilt from stored chunks (the real chunker produces them)."""

import importlib.util
import sys
from pathlib import Path

import pytest
from teacher_common import listening

ROOT = Path(__file__).resolve().parents[1]
OVERLAP = 200


@pytest.fixture(scope="module")
def real_documents():
    # conftest stubs teacher_common.documents; the real chunker is what decides how the overlap looks.
    spec = importlib.util.spec_from_file_location("real_documents", ROOT / "services/common/teacher_common/documents.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["real_documents"] = module
    spec.loader.exec_module(module)
    return module


def payloads(documents, segments, size=1200):
    chunks = documents.chunk_segments(segments, chunk_size=size, chunk_overlap=OVERLAP)
    return [
        {"position": index, "text": c.text, "locator": c.locator, "page": c.page, "chapter": c.chapter,
         "ocr": c.ocr, "ocr_confidence": c.ocr_confidence}
        for index, c in enumerate(chunks, start=1)
    ]


def sentences(count, tag="Mondat"):
    return " ".join(f"{tag} {n} a malom kerekéről és a vízről szól, ahogy a krónika följegyezte." for n in range(count))


def build(chunks, **kwargs):
    return listening.build_sections(chunks, max_overlap=OVERLAP, max_chars=300, ocr_min_confidence=70.0, **kwargs)


def test_overlapping_chunks_read_back_without_repeats(real_documents):
    text = sentences(60)
    segment = real_documents.TextSegment(text=text, locator="oldal 1", page=1)
    chunks = payloads(real_documents, [segment])
    assert len(chunks) > 3
    # shuffled: position, not arrival order, decides
    sections = build(list(reversed(chunks)))
    assert len(sections) == 1
    spoken = " ".join(s.text for s in sections[0].segments)
    assert spoken == text
    assert spoken.count("Mondat 7 ") == 1


def test_pages_and_chapters_become_sections_in_order(real_documents):
    segments = [
        real_documents.TextSegment(text=sentences(30, f"Egy{n}"), locator=f"oldal {n}", page=n) for n in (1, 2, 3)
    ]
    sections = build(payloads(real_documents, segments))
    assert [s.title for s in sections] == ["1. oldal", "2. oldal", "3. oldal"]
    assert [s.page for s in sections] == [1, 2, 3]
    assert "Egy1 29 " in sections[0].segments[-1].text
    assert not any("Egy1 " in s.text for s in sections[1].segments)

    chapters = [
        real_documents.TextSegment(text="Első fejezet\n\n" + sentences(5), locator="fejezet ch1.xhtml", chapter="ch1.xhtml"),
        real_documents.TextSegment(text=sentences(5, "Másik"), locator="fejezet ch2.xhtml", chapter="ch2.xhtml"),
    ]
    sections = build(payloads(real_documents, chapters))
    assert [s.title for s in sections] == ["Első fejezet", "ch2.xhtml"]


def test_flat_text_is_cut_at_markdown_headings_or_by_size(real_documents):
    markdown = "# Bevezetés\n\n" + sentences(3) + "\n\n## Malom\n\n" + sentences(3, "Malmos")
    sections = build(payloads(real_documents, [real_documents.TextSegment(text=markdown, locator="szövegtörzs")]))
    assert [s.title for s in sections] == ["Bevezetés", "Malom"]

    long_text = "\n\n".join(sentences(4, f"Bekezdés{n}") for n in range(60))
    sections = build(payloads(real_documents, [real_documents.TextSegment(text=long_text, locator="dokumentum")]))
    assert len(sections) > 2
    assert sections[0].title == "1. rész"
    joined = " ".join(s.text for section in sections for s in section.segments)
    assert joined.count("Bekezdés12 ") == 4  # nothing lost, nothing repeated


def test_segments_end_at_sentences_and_respect_the_limit():
    text = "Ez az első mondat. Ez a második mondat! Harmadik?\n\nÚj bekezdés kezdődik itt. " + "hosszú " * 120
    segments = listening.split_segments(text, 100)
    assert segments[0] == "Ez az első mondat. Ez a második mondat! Harmadik?"
    assert segments[1] == "Új bekezdés kezdődik itt."
    assert all(len(s) <= 100 for s in segments)
    assert len(segments) > 5


def test_pdf_line_wraps_are_joined_and_hyphenation_removed():
    text = "A kőröshegyi mal-\nom kereke nagy, és a víz\nhajtja körbe.\n\nMásik bekezdés."
    assert listening.split_segments(text, 300) == ["A kőröshegyi malom kereke nagy, és a víz hajtja körbe.", "Másik bekezdés."]


def test_low_confidence_ocr_pages_are_flagged_like_the_citations():
    chunks = [
        {"position": 1, "text": "Jól olvasható oldal szövege, elég hosszan, hogy mondat legyen.", "locator": "oldal 1 (OCR)", "page": 1, "ocr": True, "ocr_confidence": 91.0},
        {"position": 2, "text": "Elmosódott oldal, kusza betűkkel és sok hibával.", "locator": "oldal 2 (OCR)", "page": 2, "ocr": True, "ocr_confidence": 42.5},
        {"position": 3, "text": "Ismeretlen bizonyosságú oldal szövege itt áll.", "locator": "oldal 3 (OCR)", "page": 3, "ocr": True, "ocr_confidence": None},
        {"position": 4, "text": "Szöveges oldal, nem kellett OCR hozzá soha.", "locator": "oldal 4", "page": 4, "ocr": False},
    ]
    sections = build(chunks)
    flags = [(s.segments[0].ocr, s.segments[0].low_confidence) for s in sections]
    assert flags == [(True, False), (True, True), (True, True), (False, False)]
    assert sections[1].segments[0].ocr_confidence == 42.5


def test_overlap_removal_never_cuts_text_that_merely_resembles_the_tail():
    first = "x" * 150 + " ismétlődő mondatvég, amely máshol is előfordul"
    second = "ismétlődő mondatvég, amely máshol is előfordul, de itt tovább folytatódik a szöveg egészen mostanáig."
    assert listening.join_without_overlap(first, second, OVERLAP) == first + second[len("ismétlődő mondatvég, amely máshol is előfordul"):]
    unrelated = "Teljesen más kezdet, amely sehol sem szerepel az előző szövegben, ezért marad."
    assert listening.join_without_overlap(first, unrelated, OVERLAP).endswith(unrelated)
