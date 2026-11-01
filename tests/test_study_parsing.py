import json

import pytest
from teacher_common import study

HITS = [
    {"chunk_id": "c1", "source_path": "/library/a.pdf", "title": "A könyv", "locator": "12. oldal", "text": "alfa"},
    {"chunk_id": "c2", "source_path": "/library/b.txt", "title": None, "locator": "1. szakasz", "text": "béta"},
]


def quiz_json(**overrides):
    q = {
        "question": "Mi a fotoszintézis?",
        "options": ["Fényenergia megkötése", "Légzés", "Erjedés", "Párolgás"],
        "correct_index": 0,
        "explanation": "A növények fényből építenek cukrot.",
        "sources": [1],
    }
    q.update(overrides)
    return json.dumps({"questions": [q]})


def test_extract_json_handles_fences_and_prose():
    assert study.extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert study.extract_json('Íme a kvíz:\n{"a": [1, 2]} köszönöm') == {"a": [1, 2]}
    with pytest.raises(ValueError):
        study.extract_json("semmi json")


def test_parse_quiz_maps_source_numbers_to_chunks():
    result = study.parse_quiz(quiz_json(sources=[2, 1]), HITS)
    assert result.rejected == []
    refs = result.items[0]["sources"]
    assert [r["chunk_id"] for r in refs] == ["c2", "c1"]
    assert refs[0] == {
        "chunk_id": "c2",
        "file_id": None,
        "file_name": None,
        "source_path": "/library/b.txt",
        "title": None,
        "locator": "1. szakasz",
        "page": None,
        "chapter": None,
    }


def test_parse_quiz_drops_invented_source_numbers_but_keeps_valid_ones():
    result = study.parse_quiz(quiz_json(sources=[1, 99]), HITS)
    assert [r["chunk_id"] for r in result.items[0]["sources"]] == ["c1"]


@pytest.mark.parametrize(
    "overrides",
    [
        {"sources": [99]},  # only invented references
        {"sources": []},
        {"correct_index": 4},
        {"correct_index": -1},
        {"options": ["egy"]},
        {"options": ["a", "A", "b", "c"]},  # duplicates
        {"options": ["a", "", "b", "c"]},
        {"question": "?"},
    ],
)
def test_parse_quiz_rejects_invalid_items(overrides):
    result = study.parse_quiz(quiz_json(**overrides), HITS)
    assert result.items == []
    assert len(result.rejected) == 1


def test_parse_quiz_keeps_good_items_next_to_bad_ones():
    good = json.loads(quiz_json())["questions"][0]
    bad = {**good, "correct_index": 9}
    result = study.parse_quiz(json.dumps({"questions": [bad, good]}), HITS)
    assert len(result.items) == 1 and len(result.rejected) == 1


def test_parse_accepts_bare_list_and_string_source_numbers():
    cards = json.dumps([{"front": "Mi az ATP?", "back": "Energiahordozó", "sources": ["2"]}])
    result = study.parse_flashcards(cards, HITS)
    assert result.items[0]["sources"][0]["chunk_id"] == "c2"


def test_parse_wrong_shape_raises():
    with pytest.raises(ValueError):
        study.parse_flashcards(json.dumps({"questions": []}), HITS)


def run(llm, kind="quiz", **kw):
    return study.generate_items(
        kind, llm=llm, template="{level}|{count}|{topic}\n{context}", hits=HITS, level="beginner",
        count=kw.pop("count", 5), topic="téma", max_context_chars=10000, **kw,
    )


def test_generate_retries_on_malformed_output_and_tells_the_model():
    prompts = []

    def llm(prompt):
        prompts.append(prompt)
        return "ez nem json" if len(prompts) == 1 else quiz_json()

    items = run(llm)
    assert len(items) == 1 and len(prompts) == 2
    assert "nem volt használható" not in prompts[0]
    assert "nem volt használható" in prompts[1]


def test_generate_retries_when_every_item_is_invalid():
    outputs = iter([quiz_json(correct_index=7), quiz_json()])
    assert len(run(lambda p: next(outputs))) == 1


def test_generate_gives_up_after_max_attempts():
    calls = []

    def llm(prompt):
        calls.append(prompt)
        return "{}"

    with pytest.raises(study.GenerationError):
        run(llm, max_attempts=3)
    assert len(calls) == 3


def test_generate_truncates_to_requested_count():
    cards = json.dumps({"cards": [{"front": f"kérdés {i}", "back": "válasz", "sources": [1]} for i in range(5)]})
    assert len(run(lambda p: cards, kind="flashcards", count=3)) == 3


def test_context_is_numbered_and_bounded():
    context, used = study.build_context(HITS, max_chars=10000)
    assert context.splitlines()[0].startswith("[1] A könyv | 12. oldal")
    assert context.splitlines()[1].startswith("[2] b.txt | 1. szakasz")
    _, used = study.build_context(HITS, max_chars=40)
    assert len(used) == 1


def test_prompt_templates_exist_and_keep_json_braces():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "config" / "prompts"
    for kind in ("quiz", "flashcards"):
        rendered = study.render_prompt(
            study.load_prompt(root, kind), level="beginner", count=4, topic="TÉMA", context="[1] x"
        )
        assert "{level}" not in rendered and "{context}" not in rendered
        assert "TÉMA" in rendered and '"sources"' in rendered and "{" in rendered
