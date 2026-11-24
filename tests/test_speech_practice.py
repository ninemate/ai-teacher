import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from teacher_common import speech_metrics as sm


@pytest.mark.parametrize(("text", "language"), [("Um hello. What changed?", "en"), ("Hát szia. Mi változott?", "hu")])
def test_fixed_transcript(text, language):
    result = sm.analyze(text, language=language, stt={"duration": 5, "segments": [
        {"start": 0, "end": 1}, {"start": 3, "end": 4},
    ]})
    assert result.word_count == 4 and result.words_per_minute == 60
    assert result.pauses.long_count == 1 and result.pauses.total_seconds == 2
    assert result.duration_seconds == 5 and result.filler_count == 1
    assert result.question_ratio == 0.5 and result.open_question_count == 1
    typed = sm.analyze(text, language=language)
    assert typed.pauses is None and typed.words_per_minute is None


@pytest.fixture
def practice(tmp_path, monkeypatch):
    from speech_routes import build_router
    from teacher_common.config import get_settings
    from teacher_common.db import init_db

    init_db()
    monkeypatch.setattr(get_settings(), "speech_audio_dir", str(tmp_path))
    app = FastAPI()
    replies = []

    @app.middleware("http")
    async def identity(request, call_next):
        request.state.session = SimpleNamespace(username=request.headers.get("x-test-user", "speaker"))
        return await call_next(request)

    def transcribe(content, mime, language):
        assert (content, mime, language) == (b"audio", "audio/webm", "en")
        return {"text": "We save time.", "duration": 32, "segments": [{"start": 1, "end": 4}]}

    app.include_router(build_router(lambda prompt, structured: replies.pop(0), transcribe))
    return TestClient(app), replies


def test_talk_retry_and_private_history(practice):
    c, replies = practice
    talk = c.post("/speech/talks", json={"scenario": "neighbour", "language": "en", "style": "reserved"}).json()
    path = f"/speech/talks/{talk['id']}"
    replies.append("Yesterday!")
    assert c.post(path + "/turn", data={"text": "When did you move?"}).json()["turns"][-1]["text"] == "Yesterday!"
    replies.append(json.dumps({"tip": "Kérdezz vissza.", "retry_focus": "listening"}))
    assert c.post(path + "/finish").json()["metrics"]["questions"] == 1
    retry = c.post(path + "/retry").json()
    assert retry["focus"] == "listening" and retry["retry_of"] == talk["id"]
    for suffix in ("", "/retry", "/finish"):
        response = c.request("POST" if suffix else "GET", path + suffix, headers={"x-test-user": "other"})
        assert response.status_code == 404


def test_pitch_audio_comparison_and_isolation(practice):
    c, replies = practice
    data = {"scenario": "client", "target": 30, "language": "en", "interest": "high", "messages": "save time"}
    for _ in range(2):
        replies.extend([json.dumps({"tip": "Adj példát.", "structure": {
            "value": {"present": True, "quote": "save time"}
        }}), '{"questions":["How much time?"]}'])
        result = c.post("/speech/takes", data=data, files={"audio": ("a.webm", b"audio", "audio/webm")})
        assert result.status_code == 200, result.text
    take = result.json()
    assert take["feedback"]["structure"]["value"]["present"]
    assert take["comparison"] and take["metrics"]["overrun_seconds"] == 2
    path = f"/speech/takes/{take['id']}"
    assert c.get(path + "/audio").content == b"audio"
    other = {"x-test-user": "other"}
    assert c.get(path, headers=other).status_code == c.get(path + "/audio", headers=other).status_code == 404
    assert c.get("/speech/history", headers=other).json() == {"talks": [], "takes": []}


def test_conversation_structure_from_fixed_turns():
    monologue = " ".join(["project"] * 80)
    turns = [
        {"role": "partner", "text": "I just moved here from Szeged."},
        {"role": "user", "text": "Oh, Szeged! What do you miss about it?"},
        {"role": "partner", "text": "Mostly the river walks."},
        {"role": "user", "text": monologue},
        {"role": "partner", "text": "Interesting."},
        {"role": "user", "text": "Um, you know, bye then."},
    ]
    result = sm.conversation_metrics(turns, language="en")
    assert result["turns"] == 3 and result["questions"] == 1 and result["open_questions"] == 1
    assert result["question_turn_ratio"] == pytest.approx(1 / 3, abs=0.001)
    assert result["monologues"] == 1 and result["longest_turn_words"] == 80
    assert result["listening_replies"] == 3 and result["listening_references"] == 1
    assert result["fillers"] == 2


def test_hungarian_fillers_and_key_messages():
    text = "Hát, izé, szóval a csapatunk felére csökkentette az átfutási időt. Ugye?"
    assert sum(sm.count_fillers(text, "hu").values()) >= 3
    covered = sm.key_message_coverage(text, ["átfutási idő csökkentése", "új ügyfelek"], "hu")
    assert [c["covered"] for c in covered] == [True, False]


def test_feedback_drops_invented_quotes_and_scores_are_stable():
    from teacher_common import speech_practice as sp

    feedback = sp.parse_talk_feedback({"tip": "Kérdezz vissza.", "retry_focus": "nonsense", "observations": [
        {"skill": "opening", "note": "Jó nyitás.", "quote": "Hello there"},
        {"skill": "closing", "note": "Kitalált idézet.", "quote": "never said this"},
    ]}, "Hello there, how are you?", {"question_turn_ratio": 0.0})
    assert [o["skill"] for o in feedback["observations"]] == ["opening"]
    assert feedback["retry_focus"] in sp.TALK_SKILLS
    pitch = sp.parse_pitch_feedback({"tip": "Adj példát.", "structure": {"value": {"present": True, "quote": "made up"}}}, "We save time.")
    assert not pitch["structure"]["value"]["present"]
    sheet = sm.analyze("We save time.", language="en").as_dict()
    scores = sp.pitch_scores(sheet, target=30, structure=pitch["structure"], coverage=[])
    assert scores["time"] is None and scores["structure"] == 0 and scores["key_messages"] is None
    assert sp.overrun_seconds(95.4, 90) == 5.4 and sp.overrun_seconds(20, 30) == 0


def test_followup_answer_is_scored_and_private(practice):
    c, replies = practice
    replies.extend([json.dumps({"tip": "Adj példát."}), '{"questions":["How much time?","Who uses it?"]}'])
    take = c.post("/speech/takes", data={"scenario": "client", "target": 60, "language": "en",
                                         "text": "We save time for teams."}).json()
    path = f"/speech/takes/{take['id']}/answer"
    assert c.post(path, data={"index": 0, "text": "About an hour."}, headers={"x-test-user": "other"}).status_code == 404
    assert c.post(path, data={"index": 5, "text": "x"}).status_code == 404
    replies.append(json.dumps({"answered": True, "note": "Tömör.", "tip": "Mondj számot."}))
    result = c.post(path, data={"index": 0, "text": "About an hour a day."}).json()
    first, second = result["followups"]["questions"]
    assert first["answer"] == "About an hour a day." and first["feedback"]["answered"]
    assert "answer" not in second
    assert c.get(f"/speech/takes/{take['id']}").json()["followups"]["questions"][0]["feedback"]["tip"] == "Mondj számot."
