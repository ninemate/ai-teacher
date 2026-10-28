from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, inspect, text
from teacher_common import study_store as store
from teacher_common.db import Base

SRC = [{"chunk_id": "c1", "source_path": "/library/a.pdf", "title": "A", "locator": "3. oldal"}]
NOW = datetime(2026, 1, 1, 12, 0)


def questions(n=2):
    return [
        {"question": f"Kérdés {i}?", "options": ["a", "b", "c"], "correct_index": i % 3, "explanation": "x", "sources": SRC}
        for i in range(n)
    ]


def make_quiz(session, n=2):
    return store.create_quiz_set(
        session, title="T", topic="téma", level="beginner", scope_sources=["/library/a.pdf"], model="m", questions=questions(n)
    )


def make_deck(session, n=3):
    cards = [{"front": f"f{i}", "back": f"b{i}", "sources": SRC} for i in range(n)]
    return store.create_deck(
        session, title="D", topic="téma", level="beginner", scope_sources=[], model="m", cards=cards, now=NOW
    )


def test_quiz_roundtrip_keeps_sources_and_order(session):
    created = make_quiz(session, 3)
    fetched = store.get_quiz_set(session, created["id"])
    assert [q["question"] for q in fetched["questions"]] == ["Kérdés 0?", "Kérdés 1?", "Kérdés 2?"]
    assert fetched["questions"][0]["sources"] == SRC
    assert fetched["scope_sources"] == ["/library/a.pdf"]
    assert fetched["question_count"] == 3 and fetched["attempt_count"] == 0


def test_list_is_newest_first_and_delete_cascades(session):
    first, second = make_quiz(session), make_quiz(session)
    assert [q["id"] for q in store.list_quiz_sets(session)] == [second["id"], first["id"]]
    store.record_attempt(session, first["id"], {})
    store.delete_quiz_set(session, first["id"])
    session.flush()
    assert [q["id"] for q in store.list_quiz_sets(session)] == [second["id"]]
    from teacher_common.study_models import QuizAttempt, QuizQuestion

    assert session.query(QuizQuestion).filter_by(quiz_id=first["id"]).count() == 0
    assert session.query(QuizAttempt).filter_by(quiz_id=first["id"]).count() == 0
    with pytest.raises(store.NotFoundError):
        store.get_quiz_set(session, first["id"])
    with pytest.raises(store.NotFoundError):
        store.delete_quiz_set(session, first["id"])


def test_attempt_is_graded_server_side(session):
    quiz = make_quiz(session, 3)
    q0, q1, q2 = quiz["questions"]
    attempt = store.record_attempt(
        session, quiz["id"], {q0["id"]: q0["correct_index"], q1["id"]: (q1["correct_index"] + 1) % 3}
    )
    assert (attempt["score"], attempt["total"]) == (1, 3)  # q2 unanswered
    assert attempt["answers"][str(q0["id"])]["correct"] is True
    assert attempt["answers"][str(q2["id"])] == {"chosen": None, "correct": False}
    summary = store.get_quiz_set(session, quiz["id"])
    assert summary["attempt_count"] == 1 and summary["last_score"] == 1
    assert store.list_attempts(session, quiz["id"])[0]["id"] == attempt["id"]


def test_attempt_rejects_bad_input(session):
    quiz = make_quiz(session, 1)
    qid = quiz["questions"][0]["id"]
    with pytest.raises(store.InvalidInputError):
        store.record_attempt(session, quiz["id"], {qid + 1000: 0})
    with pytest.raises(store.InvalidInputError):
        store.record_attempt(session, quiz["id"], {qid: 9})
    with pytest.raises(store.NotFoundError):
        store.record_attempt(session, 999, {})


def test_deck_cards_start_due_and_review_reschedules(session):
    deck = make_deck(session)
    assert deck["card_count"] == 3 and deck["due_count"] == 3
    card = store.due_cards(session, deck["id"], now=NOW)[0]
    reviewed = store.review_card(session, card["id"], "good", now=NOW)
    assert reviewed["interval_days"] == 1 and reviewed["repetitions"] == 1
    assert reviewed["due_at"] == (NOW + timedelta(days=1)).isoformat()
    assert reviewed["last_reviewed_at"] == NOW.isoformat()
    assert len(store.due_cards(session, deck["id"], now=NOW)) == 2
    assert store.get_deck(session, deck["id"], now=NOW)["due_count"] == 2
    # a day later it is due again
    assert len(store.due_cards(session, deck["id"], now=NOW + timedelta(days=1))) == 3
    assert store.get_deck(session, deck["id"])["cards"][0]["sources"] == SRC


def test_review_again_keeps_card_in_session(session):
    deck = make_deck(session, 1)
    card = deck["cards"][0]
    reviewed = store.review_card(session, card["id"], "again", now=NOW)
    assert reviewed["lapses"] == 1
    assert store.due_cards(session, deck["id"], now=NOW) == []
    assert len(store.due_cards(session, deck["id"], now=NOW + timedelta(minutes=11))) == 1


def test_review_rejects_unknown_grade_and_card(session):
    deck = make_deck(session, 1)
    with pytest.raises(store.InvalidInputError):
        store.review_card(session, deck["cards"][0]["id"], "perfect")
    with pytest.raises(store.NotFoundError):
        store.review_card(session, 12345, "good")


def test_delete_deck_removes_cards(session):
    deck = make_deck(session)
    store.delete_deck(session, deck["id"])
    session.flush()
    from teacher_common.study_models import Flashcard

    assert session.query(Flashcard).count() == 0
    with pytest.raises(store.NotFoundError):
        store.due_cards(session, deck["id"])


# --- migrations ---


def test_init_on_legacy_database_adds_study_tables_and_keeps_data():
    from teacher_common import models, study_models  # noqa: F401
    from teacher_common.migrations import MIGRATIONS, run_migrations

    engine = create_engine("sqlite://", future=True)
    # a database as created before this feature: only the original tables, with a row in it
    legacy = [t for t in Base.metadata.sorted_tables if t.name in {"documents", "chunks", "ingest_runs"}]
    Base.metadata.create_all(engine, tables=legacy)
    with engine.begin() as c:
        c.execute(text("INSERT INTO ingest_runs (status, scanned, indexed, skipped, failed, deleted) VALUES ('done',1,1,0,0,0)"))
    assert "quiz_sets" not in inspect(engine).get_table_names()

    Base.metadata.create_all(engine)
    assert run_migrations(engine) == [m[0] for m in MIGRATIONS]

    tables = set(inspect(engine).get_table_names())
    assert {"quiz_sets", "quiz_questions", "quiz_attempts", "flashcard_decks", "flashcards", "schema_migrations"} <= tables
    with engine.connect() as c:
        assert c.execute(text("SELECT status FROM ingest_runs")).scalar() == "done"


def test_migrations_apply_once_and_in_order():
    from teacher_common.migrations import run_migrations

    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(engine)
    log = []

    def step(n):
        return lambda connection: log.append(n)

    migrations = [(2, "two", step(2)), (1, "one", step(1))]
    assert run_migrations(engine, migrations) == [1, 2]
    assert run_migrations(engine, migrations) == []
    migrations.append((3, "three", step(3)))
    assert run_migrations(engine, migrations) == [3]
    assert log == [1, 2, 3]


def test_failed_migration_is_not_recorded():
    from teacher_common.migrations import run_migrations

    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(engine)

    def boom(connection):
        raise RuntimeError("nope")

    with pytest.raises(RuntimeError):
        run_migrations(engine, [(1, "bad", boom)])
    assert run_migrations(engine, [(1, "good", lambda c: None)]) == [1]
