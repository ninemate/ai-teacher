"""The Document Intelligence engine with a fake model: windows, grounding, retries, resume, comparison, export."""

import csv
import io
import json

import pytest
from docintel_fakes import INVENTED, FakeModel, make_doc, make_runner
from teacher_common import doc_text, docintel

LONG = "Mondat a dokumentumból, amely hosszabb szöveg része. " * 8  # ~400 characters


def long_doc(name="hosszu.pdf", pages=12, **kwargs):
    return make_doc(name, [f"{index}. oldal tartalma. {LONG}" for index in range(1, pages + 1)], **kwargs)


def run(kind, documents, model=None, options=None, **runner_kwargs):
    model = model or FakeModel()
    runner = make_runner(model, **runner_kwargs)
    return docintel.run_job(kind, runner, documents, options or {}), model, runner


# --- reading order and windows -------------------------------------------------------------------------------------


def test_text_is_rebuilt_in_reading_order_without_the_chunk_overlap():
    shared = "ez a mondat a két chunk közös, átfedő vége"
    payloads = [
        {"chunk_id": "c2", "position": 2, "page": 1, "text": shared + " és itt folytatódik", "source_path": "/l/a.pdf"},
        {"chunk_id": "c1", "position": 1, "page": 1, "text": "Eleje a szövegnek, " + shared, "source_path": "/l/a.pdf"},
        {"chunk_id": "c3", "position": 3, "page": 2, "text": "Új oldal, nincs átfedés.", "source_path": "/l/a.pdf"},
    ]
    # the vector store returns them sorted; load_chunks must also cope with trimming only inside one page
    chunks = doc_text.load_chunks("/l/a.pdf", {"w"}, lambda path, ws: sorted(payloads, key=lambda p: p["position"]))
    assert [c["chunk_id"] for c in chunks] == ["c1", "c2", "c3"]
    assert chunks[1]["text"] == "és itt folytatódik"
    assert chunks[2]["text"] == "Új oldal, nincs átfedés."


def test_windows_cover_every_chunk_once_in_order_within_the_budget():
    document = long_doc(pages=12)
    windows = doc_text.make_windows(document["chunks"], 1500)
    flat = [chunk["chunk_id"] for window in windows for chunk in window]
    assert flat == [chunk["chunk_id"] for chunk in document["chunks"]]
    assert len(windows) > 1
    for window in windows:
        assert sum(len(c["text"]) for c in window) <= 1500


def test_a_chunk_larger_than_the_window_budget_is_cut_into_pieces_that_fit_and_keep_their_source():
    document = long_doc(pages=3)
    budget = 400
    assert any(doc_text.chunk_cost(c) > budget for c in document["chunks"])
    pieces = doc_text.split_oversized(document["chunks"], budget)
    assert all(doc_text.chunk_cost(p) <= budget for p in pieces)
    assert len({p["chunk_id"] for p in pieces}) == len(pieces)
    # nothing lost, nothing reordered, page attribution kept
    assert " ".join(" ".join(p["text"].split()) for p in pieces) == " ".join(" ".join(c["text"].split()) for c in document["chunks"])
    assert [p["page"] for p in pieces] == sorted(p["page"] for p in pieces)
    assert {p["page"] for p in pieces} == {1, 2, 3}
    # a budget just above the numbered line's overhead still gets pieces that fit, however short
    for tight in (doc_text.LINE_OVERHEAD + 200, 800):
        tiny = doc_text.split_oversized(document["chunks"], tight)
        assert all(doc_text.chunk_cost(p) <= tight for p in tiny), tight
    # a budget the chunks already fit leaves them as they are
    assert doc_text.split_oversized(document["chunks"], 10_000) == document["chunks"]
    # and the windows made from the pieces respect the budget
    assert all(sum(doc_text.chunk_cost(c) for c in w) <= budget for w in doc_text.make_windows(pieces, budget))


def test_a_window_setting_too_small_for_the_numbered_line_is_rejected():
    from pydantic import ValidationError
    from teacher_common.config import MIN_DOCINTEL_WINDOW, Settings

    for name in ("DOCINTEL_WINDOW_CHARS", "DOCINTEL_TRANSLATE_CHARS"):
        with pytest.raises(ValidationError):
            Settings(**{name: MIN_DOCINTEL_WINDOW - 1})
        assert Settings(**{name: MIN_DOCINTEL_WINDOW})
        assert Settings(**{name: 0})  # unset: derived from the context size


def test_the_cost_of_a_chunk_covers_its_whole_numbered_context_line_whatever_its_place():
    from teacher_common.citations import Limits, build_citations, build_context

    limits = Limits(weak_score=0.8, ocr_min_confidence=70)
    base = {"chunk_id": "c", "file_name": "jegyzet.txt", "source_path": "/k/jegyzet.txt", "title": "Cím", "score": None,
            "text": "Mondat a dokumentumból. " * 60}
    cases = [
        {**base, "page": None, "chapter": None, "locator": "L" * 255},  # a page-less, chapter-less, long locator
        {**base, "page": 12, "chapter": None, "locator": None},
        {**base, "page": None, "chapter": "A" * 100, "locator": None, "ocr": True, "ocr_confidence": 12.0},
        {**base, "page": None, "chapter": None, "locator": None, "ocr": True},
        {**base, "file_name": None, "page": 3, "chapter": None, "locator": None},
    ]
    for chunk in cases:
        context, _ = build_context(build_citations([chunk], limits), [chunk], 10**9)
        assert len(context) <= doc_text.chunk_cost(chunk), chunk
        # so a window at the smallest allowed budget really holds its numbered context
        for piece_window in doc_text.make_windows(doc_text.split_oversized([chunk], 800), 800):
            built = build_citations(piece_window, limits)
            rendered, used = build_context(built, piece_window, 10**9)
            assert len(used) == len(piece_window) and len(rendered) <= 800, chunk


# --- summary -------------------------------------------------------------------------------------------------------


def test_summary_reads_the_whole_document_and_every_source_point_is_real():
    document = long_doc(pages=12)
    result, model, runner = run("summary", [document])
    windows = doc_text.make_windows(document["chunks"], runner.window_chars)
    # every window was read: the model saw each page of the document, not retrieved snippets
    assert model.count("summary_map") == len(windows) > 3
    seen = "\n".join(prompt for kind, prompt in model.calls if kind == "summary_map")
    assert all(f"{page}. oldal tartalma" in seen for page in range(1, 13))
    # the reduce step ran hierarchically and every prompt stays bounded by the configured window
    assert model.count("summary_reduce") >= 1
    assert all(len(prompt) < runner.window_chars + 3000 for _, prompt in model.calls)
    ids = {c["id"] for c in result["citations"]}
    assert result["overall"] and all(p["sources"] and set(p["sources"]) <= ids for p in result["overall"])
    assert INVENTED not in ids
    # citations carry file and page, like chat answers
    assert {c["label"] for c in result["citations"]} <= {f"hosszu.pdf, {n}. oldal" for n in range(1, 13)}
    assert result["grounding"]["status"] == "grounded"
    assert result["coverage"]["windows"] == len(windows) and result["coverage"]["failed"] == 0


def test_summary_per_chapter_has_a_section_for_each_chapter():
    document = long_doc("konyv.epub", pages=4, chapters=True)
    result, model, _ = run("summary", [document], options={"granularity": "chapter"})
    assert [s["label"] for s in result["sections"]] == ["1. fejezet", "2. fejezet", "3. fejezet", "4. fejezet"]
    assert all(s["points"] for s in result["sections"])
    assert result["overall"]


def test_a_point_citing_only_an_invented_number_is_dropped_not_shown_as_sourced():
    model = FakeModel()
    model.script = [json.dumps({"points": [
        {"text": "Kitalált állítás", "sources": [INVENTED]},
        {"text": "Alátámasztott állítás", "sources": [1]},
    ]})]
    result, _, _ = run("summary", [make_doc("a.pdf", ["Rövid szöveg a dokumentumban."])], model)
    assert [p["text"] for p in result["overall"]] == ["Alátámasztott állítás"]
    assert result["coverage"]["rejected_items"] == 1
    assert "elvetettem" in result["notice"]


# --- unusable model output -----------------------------------------------------------------------------------------


def test_output_that_is_not_json_is_retried_once_then_used():
    model = FakeModel()
    model.script = ["Sajnos ez nem JSON, hanem szöveg."]
    result, model, _ = run("summary", [make_doc("a.pdf", ["Rövid szöveg a dokumentumban."])], model)
    assert model.count("summary_map") == 2
    assert "nem volt használható" in model.calls[1][1]
    assert result["overall"]


def test_a_job_whose_output_is_never_usable_fails_clearly():
    model = FakeModel()
    model.script = ["nem json", "ez sem json"]
    with pytest.raises(docintel.JobFailed, match="nem adott használható kimenetet"):
        run("summary", [make_doc("a.pdf", ["Rövid szöveg a dokumentumban."])], model)
    assert model.count("summary_map") == 2  # one try and exactly one retry


def test_a_window_the_model_cannot_answer_is_reported_not_hidden():
    document = long_doc(pages=6)
    model = FakeModel()
    model.script = ["x", "y"]  # the first window fails both tries; the rest answer normally
    result, _, _ = run("summary", [document], model)
    assert result["coverage"]["failed"] == 1
    assert result["coverage"]["failed_places"][0].startswith("hosszu.pdf, 1. oldal")
    assert "nem sikerült feldolgozni" in result["notice"]


# --- extraction ----------------------------------------------------------------------------------------------------


EXTRACT_DOC = [
    "Követelmény: A jelentkezőnek érettségi bizonyítvány szükséges. Határidő: 2026. március 1-ig kell jelentkezni.",
    "Kötelezettség: A hallgató köteles havonta beszámolni. Fogalom: A kredit a tanulmányi teljesítmény mértéke. "
    "Táblázat: díjak.",
    "Követelmény: A jelentkezőnek érettségi bizonyítvány szükséges.",
]


def test_extract_rows_have_type_source_file_and_page_and_exports_keep_them():
    document = make_doc("szabalyzat.pdf", EXTRACT_DOC)
    result, _, _ = run("extract", [document], options={"types": list(docintel.EXTRACT_TYPES)})
    by_type = {}
    for row in result["rows"]:
        by_type.setdefault(row["type"], []).append(row)
    assert set(by_type) == {"requirement", "deadline", "obligation", "concept", "table"}
    # the repeated requirement (pages 1 and 3) is one row citing both pages
    requirement = by_type["requirement"][0]
    assert len(by_type["requirement"]) == 1 and len(requirement["sources"]) == 2
    labels = {c["id"]: c["label"] for c in result["citations"]}
    assert {labels[i] for i in requirement["sources"]} == {"szabalyzat.pdf, 1. oldal", "szabalyzat.pdf, 3. oldal"}
    assert INVENTED not in labels
    assert by_type["table"][0]["table"]["rows"] == [["Tagdíj", "1000"]]

    rows = list(csv.reader(io.StringIO(docintel.export_csv(result).lstrip("﻿"))))
    assert rows[0][:4] == ["Típus", "Tartalom", "Részlet", "Forrás (fájl, oldal/fejezet)"]
    assert all(row[3] and "szabalyzat.pdf" in row[3] for row in rows[1:])
    assert any("Tétel=Tagdíj; Összeg=1000" in row[2] for row in rows[1:])
    markdown = docintel.export_markdown(result, "Szabályzat")
    assert "| Típus |" in markdown and "szabalyzat.pdf, 1. oldal" in markdown and "| Tagdíj | 1000 |" in markdown


def _table(rows):
    return {"type": "table", "text": "Díjak", "detail": "", "sources": [1], "table": {"columns": ["Tétel", "Összeg"], "rows": rows}}


def test_tables_under_the_same_caption_are_not_merged_unless_their_cells_are_the_same():
    document = make_doc("dijak.pdf", ["Díjak első táblázata.", "Díjak második táblázata.", "Díjak első táblázata."])
    answers = [
        json.dumps({"items": [_table([["Tagdíj", "1000"]])]}),
        json.dumps({"items": [_table([["Belépő", "500"], ["Kaució", "200"]])]}),
        json.dumps({"items": [_table([["Tagdíj", "1000"]])]}),
    ]
    model = FakeModel()
    model.script = answers
    result, _, _ = run("extract", [document], model, options={"types": ["table"]}, window_chars=30)
    tables = [row for row in result["rows"] if row["type"] == "table"]
    assert [t["table"]["rows"] for t in tables] == [[["Tagdíj", "1000"]], [["Belépő", "500"], ["Kaució", "200"]]]
    first = tables[0]
    assert len(first["sources"]) == 2  # the identical table seen twice is one table citing both pages


def test_requirements_with_the_same_text_but_different_details_stay_apart_and_equal_ones_merge():
    document = make_doc("szabaly.pdf", ["Első oldal.", "Második oldal.", "Harmadik oldal."])

    def requirement(detail, source=1):
        return {"type": "requirement", "text": "A tagok beszámolót adnak.", "detail": detail, "sources": [source]}

    model = FakeModel()
    model.script = [
        json.dumps({"items": [requirement("határidő: március 15.")]}),
        json.dumps({"items": [requirement("határidő: június 30.")]}),
        json.dumps({"items": [requirement("Határidő:  március 15!")]}),  # same detail, other case and punctuation
    ]
    result, _, _ = run("extract", [document], model, options={"types": ["requirement"]}, window_chars=30)
    details = [row["detail"] for row in result["rows"]]
    assert details == ["határidő: március 15.", "határidő: június 30."]
    assert len(result["rows"][0]["sources"]) == 2 and len(result["rows"][1]["sources"]) == 1


def test_a_table_with_rows_of_the_wrong_width_is_unusable_output_and_asked_for_again():
    document = make_doc("dijak.pdf", ["Díjak táblázata."])
    model = FakeModel()
    model.script = [
        json.dumps({"items": [_table([["Tagdíj", "1000", "felesleges"]])]}),
        json.dumps({"items": [_table([["Tagdíj", "1000"]])]}),
    ]
    result, _, _ = run("extract", [document], model, options={"types": ["table"]})
    assert model.count("extract") == 2
    (row,) = result["rows"]
    assert row["table"]["rows"] == [["Tagdíj", "1000"]]
    assert all(len(r) == len(row["table"]["columns"]) for r in row["table"]["rows"])


def test_extract_only_returns_the_requested_types():
    document = make_doc("szabalyzat.pdf", EXTRACT_DOC)
    result, _, _ = run("extract", [document], options={"types": ["deadline"]})
    assert {row["type"] for row in result["rows"]} == {"deadline"}


def test_csv_export_neutralises_spreadsheet_formulas():
    document = make_doc("a.pdf", ["Követelmény: =HYPERLINK(\"http://x\", \"katt\") a szabály."])
    result, _, _ = run("extract", [document])
    body = docintel.export_csv(result)
    assert "'=HYPERLINK" in body and "\n=HYPERLINK" not in body and ",=HYPERLINK" not in body


def test_extract_with_nothing_to_find_says_so():
    result, _, _ = run("extract", [make_doc("a.pdf", ["Semmi különös, csak egy mondat."])])
    assert result["rows"] == [] and "nem sikerült egyetlen elemet sem kigyűjteni" in result["notice"]


# --- compare and gap -----------------------------------------------------------------------------------------------

A_DOC = [
    "Követelmény: A pályázó legalább ötéves szakmai tapasztalattal rendelkezzen. "
    "Követelmény: A beadványt két példányban kell benyújtani. "
    "Határidő: A jelentkezés március tizenötödikéig lehetséges.",
    "Követelmény: Az adatokat titkosítva kell tárolni. Kötelezettség: A pályázó évente jelentést készít.",
]
B_DOC = [
    "Követelmény: A pályázó legalább nyolcéves szakmai tapasztalattal rendelkezzen. "
    "Követelmény: A beadványt két példányban kell benyújtani. "
    "Kötelezettség: A pályázó évente jelentést készít.",
    "Követelmény: Új szabály a külső partnerekről szóló nyilatkozat. EKVIVALENS jelölés nincs.",
]


def test_compare_marks_changes_and_additions_with_sources_on_both_sides():
    a, b = make_doc("regi.pdf", A_DOC), make_doc("uj.pdf", B_DOC)
    result, model, _ = run("compare", [a, b], options={"mode": "diff"})
    kinds = {f["kind"]: f for f in result["findings"]}
    # the unchanged requirement and obligation are not reported
    texts = " ".join((f["a"] or {}).get("text", "") + (f["b"] or {}).get("text", "") for f in result["findings"])
    assert "két példányban" not in texts and "évente jelentést" not in texts
    changed = kinds["changed"]
    assert changed["status"] == "evidenced"
    labels = {c["id"]: c["label"] for c in result["citations"]}
    assert labels[changed["a"]["sources"][0]] == "regi.pdf, 1. oldal"
    assert labels[changed["b"]["sources"][0]] == "uj.pdf, 1. oldal"
    # absent from the other document's whole text: evidenced; nothing is built on an invented candidate number
    assert kinds["only_in_b"]["status"] == "evidenced"
    assert all(f["status"] in ("evidenced", "uncertain") for f in result["findings"])
    assert "kitalált" not in json.dumps(result, ensure_ascii=False)
    assert model.count("compare") >= 1


def test_gap_reports_only_what_the_reference_requires_and_the_other_lacks():
    a, b = make_doc("elvaras.pdf", A_DOC), make_doc("ajanlat.pdf", B_DOC)
    result, _, _ = run("compare", [a, b], options={"mode": "gap"})
    assert result["mode"] == "gap"
    # never what only B has; the requirement B repeats with another number is a partial match, not silently dropped
    assert {f["kind"] for f in result["findings"]} == {"missing_in_b", "changed"}
    missing = " ".join(f["a"]["text"] for f in result["findings"] if f["kind"] == "missing_in_b")
    assert "titkosítva" in missing or "tizenötödikéig" in missing
    assert all(f["b"] is None for f in result["findings"] if f["kind"] == "missing_in_b")
    (partial,) = [f for f in result["findings"] if f["kind"] == "changed"]
    assert "ötéves" in partial["a"]["text"] and "nyolcéves" in partial["b"]["text"]
    assert partial["status"] == "evidenced" and partial["b"]["sources"]


def test_a_partial_match_in_gap_mode_that_the_model_did_not_judge_stays_uncertain():
    a, b = make_doc("elvaras.pdf", A_DOC), make_doc("ajanlat.pdf", B_DOC)
    model = FakeModel()
    model.answer_compare = lambda prompt: "ez nem json"
    result, _, _ = run("compare", [a, b], model, options={"mode": "gap"})
    partial = [f for f in result["findings"] if f["kind"] == "changed"]
    assert len(partial) == 1 and partial[0]["status"] == "uncertain"


def test_a_gap_that_may_only_be_worded_differently_is_uncertain_not_evidenced():
    a = make_doc("elvaras.pdf", ["Követelmény: Az adatokat titkosítva kell tárolni."])
    b = make_doc("ajanlat.pdf", ["Az adatok tárolása titkosítva történik, titkosítva kell tárolni őket a szerveren."])
    result, _, _ = run("compare", [a, b], options={"mode": "gap"})
    (finding,) = result["findings"]
    assert finding["status"] == "uncertain" and finding["closest"]["sources"]


def test_findings_the_model_did_not_judge_stay_uncertain():
    a, b = make_doc("regi.pdf", A_DOC), make_doc("uj.pdf", B_DOC)
    model = FakeModel()
    model.answer_compare = lambda prompt: "ez nem json"
    result, _, _ = run("compare", [a, b], model, options={"mode": "diff"})
    assert result["findings"] and all(f["status"] == "uncertain" for f in result["findings"])
    assert "nem értékelte" in result["notice"]


def test_compare_needs_two_documents():
    with pytest.raises(docintel.JobFailed):
        run("compare", [make_doc("a.pdf", A_DOC)])


# --- tasks ---------------------------------------------------------------------------------------------------------


def test_tasks_keep_conclusions_apart_from_source_data():
    document = make_doc("projekt.pdf", [
        "Teendő: Az árajánlatot be kell nyújtani. Teendő: A szerződést alá kell írni.",
        "Teendő: A jelentést el kell küldeni.",
    ])
    result, _, _ = run("tasks", [document])
    assert [r["text"] for r in result["rows"]] == [
        "Az árajánlatot be kell nyújtani.", "A szerződést alá kell írni.", "A jelentést el kell küldeni."]
    assert all(r["sources"] for r in result["rows"])
    conclusions = result["conclusions"]
    assert conclusions and all(c["own"] for c in conclusions)
    grounded = [c for c in conclusions if c["grounded_in"]]
    ungrounded = [c for c in conclusions if not c["grounded_in"]]
    assert grounded and all(c["sources"] for c in grounded)
    # the proposal citing an invented number has no basis and no sources: shown as the model's own
    assert ungrounded and all(c["sources"] == [] for c in ungrounded)
    assert all(c["text"] not in [r["text"] for r in result["rows"]] for c in conclusions)
    assert result["grounding"]["status"] == "partial"
    assert "saját javaslatai" in result["notice"]
    header, rows = docintel.table_view(result)
    assert {row[0] for row in rows} == {"Forrásadat", "Következtetés (a modell javaslata)"}


# --- translation ---------------------------------------------------------------------------------------------------


def test_translation_covers_the_whole_document_and_points_back_to_the_pages():
    document = long_doc(pages=6)
    result, model, runner = run("translate", [document], options={"direction": "hu-en"})
    assert result["direction"] == "hu-en" and model.count("translate_hu_en") == len(result["segments"]) > 1
    joined = "\n".join(s["text"] for s in result["segments"])
    assert all(f"{page}. oldal tartalma" in joined for page in range(1, 7))
    assert all(s["text"].startswith("[EN] ") and s["sources"] for s in result["segments"])
    ids = {c["id"] for c in result["citations"]}
    assert all(set(s["sources"]) <= ids for s in result["segments"])
    assert docintel.export_text(result).count("[EN] ") == len(result["segments"])
    assert "hosszu.pdf" in docintel.export_markdown(result, "Fordítás")
    with pytest.raises(ValueError):
        docintel.export_text({"kind": "extract"})


def test_translation_direction_picks_the_prompt():
    result, model, _ = run("translate", [make_doc("a.pdf", ["Hello world, this is the text."])],
                           options={"direction": "en-hu"})
    assert model.count("translate_en_hu") == 1 and result["segments"][0]["text"].startswith("[HU] ")


def test_a_translation_with_a_hole_fails_instead_of_skipping_it():
    model = FakeModel()
    model.script = ["", ""]  # empty twice: the window cannot be translated
    with pytest.raises(docintel.JobFailed, match="fordítás nem sikerült"):
        run("translate", [make_doc("a.pdf", ["Szöveg, amelyet le kellene fordítani."])], model,
            options={"direction": "hu-en"})


# --- resume and cancel ---------------------------------------------------------------------------------------------


def test_a_resumed_job_does_not_redo_finished_steps():
    document = long_doc(pages=8)
    parts: dict = {}
    first = FakeModel()
    first.fail_on = 4  # the process "dies" on the 4th model call
    with pytest.raises(RuntimeError):
        docintel.run_job("summary", make_runner(first, parts=parts), [document], {})
    done = len(parts)
    assert done == 3  # three steps finished and were stored before the crash
    second = FakeModel()
    result = docintel.run_job("summary", make_runner(second, parts=parts), [document], {})
    full = FakeModel()
    reference = docintel.run_job("summary", make_runner(full), [document], {})
    assert second.count("summary_map") == full.count("summary_map") - 3
    assert result["overall"] == reference["overall"]


def test_cancel_stops_between_steps():
    document = long_doc(pages=8)
    model = FakeModel()
    runner = make_runner(model, should_cancel=lambda: len(model.calls) >= 2)
    with pytest.raises(docintel.JobCancelled):
        docintel.run_job("summary", runner, [document], {})
    assert len(model.calls) == 2


def test_progress_is_reported_per_step():
    seen = []
    model = FakeModel()
    runner = make_runner(model, on_progress=lambda done, total, phase: seen.append((done, total)))
    docintel.run_job("extract", runner, [long_doc(pages=6)], {})
    assert seen[-1][0] == seen[-1][1] > 1
    assert [d for d, _ in seen] == sorted(d for d, _ in seen)
