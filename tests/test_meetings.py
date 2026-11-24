"""Meetingek: chunked transcription, grounded notes, explicit assignees, exports and per-user isolation."""

import io
import json
import re
import shutil
import sys
import wave

import pytest
from fastapi.testclient import TestClient
from teacher_common import meeting_notes
from teacher_common.docintel import OutputError, Runner
from teacher_common.meeting_audio import AudioDecoder

PASSWORDS = {"apa": "helyes-jelszo-123", "anya": "masik-jelszo-456"}
SEGMENT_LINE = re.compile(r"^\[(\d+)\] ", re.MULTILINE)


def make_wav(seconds: float, rate: int = 16000) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setparams((1, 2, rate, 0, "NONE", ""))
        audio.writeframes(b"\x00\x00" * int(seconds * rate))
    return output.getvalue()


def wav_seconds(data: bytes) -> float:
    with wave.open(io.BytesIO(data), "rb") as audio:
        return audio.getnframes() / audio.getframerate()


def fake_transcribe(calls: list):
    """Two timed segments per chunk, the way the voice service answers, with chunk-relative times."""
    def transcribe(wav: bytes) -> dict:
        calls.append(wav_seconds(wav))
        index = len(calls)
        return {"text": "", "segments": [
            {"start": 0.5, "end": 4.0, "text": f" Részlet {index}: a jelentést péntekig elkészítjük. "},
            {"start": 5.0, "end": 9.0, "text": f"Részlet {index}: kérdés, hogy ki hagyja jóvá?"},
        ]}
    return transcribe


def fake_llm(prompt: str, json_mode: bool) -> str:
    ids = [int(n) for n in SEGMENT_LINE.findall(prompt)]
    if "részösszefoglalókat" in prompt:
        cited = [int(n) for n in re.findall(r"\d+", prompt.split("\n\n")[-1])][:1] or [1]
        return json.dumps({"summary": [{"text": "Összevont pont", "segments": cited}]})
    first = ids[0]
    return json.dumps({
        "summary": [{"text": f"Összefoglaló {first}", "segments": [first]}],
        "decisions": [{"text": "A jelentés péntekig elkészül.", "segments": [first, 999]}],
        "questions": [{"text": "Ki hagyja jóvá?", "segments": [ids[-1]]}],
        # 999 is an invented segment: it must be dropped, and an item with no real source rejected.
        "actions": [{"text": "Jelentés elkészítése", "segments": [first]}, {"text": "Kitalált", "segments": [999]}],
    })


def runner(llm, window_chars=4800) -> Runner:
    prompts = {"map": "Leirat:\n{context}", "reduce": "részösszefoglalókat\n\n{context}"}
    return Runner(llm=llm, prompts=prompts.__getitem__, limits=None, window_chars=window_chars, translate_chars=0,
                  max_attempts=2)


SEGMENTS = [
    {"id": 1, "start": 0.5, "end": 4.0, "text": "Péntekig kész a jelentés.", "speaker": None},
    {"id": 2, "start": 3725.25, "end": 3730.0, "text": "Ki hagyja jóvá?", "speaker": None},
]


# --- notes and exports ------------------------------------------------------------------------------------------------


def test_notes_keep_only_real_segment_sources_and_retry_unusable_output():
    answers = iter([
        "Sajnos nem tudok segíteni.",
        json.dumps({
            "summary": [{"text": "Jelentés péntekig", "segments": [1, 2, 999]}],
            "decisions": [{"text": "Kitalált döntés", "segments": [999]}],
            "questions": [{"text": "Ki hagyja jóvá?", "segments": [2]}],
            "actions": [{"text": "Jelentés elkészítése", "segments": [1], "assignee": "Béla"}],
        }),
    ])
    prompts = []
    notes = meeting_notes.summarize(SEGMENTS, runner(lambda p, j: prompts.append(p) or next(answers)))
    assert notes["summary"] == [{"text": "Jelentés péntekig", "segments": [1, 2]}]
    # An item that cites no real transcript line is not shown.
    assert notes["decisions"] == []
    # The model never sets an owner: a name it volunteers is not kept.
    assert notes["actions"] == [{"text": "Jelentés elkészítése", "segments": [1]}]
    assert notes["gaps"] == []
    assert "nem volt használható" in prompts[1]
    assert "[2] 01:02:05.250 Ki hagyja jóvá?" in prompts[0]


def test_a_window_the_model_cannot_handle_becomes_a_gap_not_a_failure():
    segments = [{"id": i, "start": i * 10.0, "end": i * 10.0 + 9, "text": "szó " * 60, "speaker": None}
                for i in range(1, 21)]

    def llm(prompt, json_mode):
        return "nem JSON" if "[1] " in prompt else fake_llm(prompt, json_mode)

    notes = meeting_notes.summarize(segments, runner(llm, window_chars=1200))
    assert notes["gaps"] and notes["gaps"][0]["start"] == 10.0
    assert notes["actions"]
    assert "nem készült jegyzet: 00:00:10.000" in meeting_notes.export_notes("m.wav", segments, notes)

    with pytest.raises(OutputError):
        meeting_notes.summarize(segments, runner(lambda p, j: "nem JSON", window_chars=1200))


def test_long_transcripts_are_mapped_in_windows_and_reduced_to_three_points():
    segments = [{"id": i, "start": i * 10.0, "end": i * 10.0 + 9, "text": "szó " * 60, "speaker": None}
                for i in range(1, 121)]
    calls = []

    def llm(prompt, json_mode):
        calls.append(prompt)
        return fake_llm(prompt, json_mode)

    notes = meeting_notes.summarize(segments, runner(llm, window_chars=1200))
    windows = [c for c in calls if "részösszefoglalókat" not in c]
    assert len(windows) > 10
    assert all(len(c) < 1400 for c in windows)
    assert 1 <= len(notes["summary"]) <= 3
    # Duplicates across windows collapse into one item that keeps all of its sources.
    assert len(notes["decisions"]) == 1 and len(notes["decisions"][0]["segments"]) > 1


def test_exports_transcript_as_text_and_srt_and_notes_as_markdown():
    srt = meeting_notes.export_transcript(SEGMENTS, srt=True)
    assert srt.startswith("1\n00:00:00,500 --> 00:00:04,000\nPéntekig kész a jelentés.\n\n2\n01:02:05,250 --> ")
    text = meeting_notes.export_transcript(SEGMENTS, srt=False)
    assert "[01:02:05.250] Ismeretlen beszélő — Ki hagyja jóvá?" in text
    notes = {
        "summary": [{"text": "Jelentés *péntekig*", "segments": [1]}], "decisions": [], "questions": [],
        "actions": [{"text": "Jóváhagyás", "segments": [2], "assignee": "Kiss [Anna]"},
                    {"text": "Küldés", "segments": [1], "assignee": None}],
    }
    markdown = meeting_notes.export_notes("heti_megbeszélés.m4a", SEGMENTS, notes)
    assert markdown.startswith("# heti\\_megbeszélés\\.m4a\n")
    assert "- Jelentés \\*péntekig\\* (00:00:00.500)" in markdown
    assert "- Jóváhagyás — Felelős: Kiss \\[Anna\\] (01:02:05.250)" in markdown
    assert "- Küldés (00:00:00.500)" in markdown
    assert "## Döntések\n\nNincs ilyen bejegyzés." in markdown


# --- audio chunking -------------------------------------------------------------------------------------------------


def test_wav_is_cut_into_bounded_chunks_without_ffmpeg(tmp_path):
    source = tmp_path / "felvetel.wav"
    source.write_bytes(make_wav(100))
    decoder = AudioDecoder()
    decoder.ffmpeg = None
    assert decoder.extensions == (".wav",)
    assert decoder.duration(source) == pytest.approx(100)
    chunks = list(decoder.chunks(source, 45, lambda: None))
    assert [c.offset for c in chunks] == pytest.approx([0, 45, 90])
    assert [wav_seconds(c.wav) for c in chunks] == pytest.approx([45, 45, 10])


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg is not installed")
def test_ffmpeg_resamples_to_16k_mono_chunks(tmp_path):
    source = tmp_path / "felvetel.wav"
    stereo = io.BytesIO()
    with wave.open(stereo, "wb") as audio:
        audio.setparams((2, 2, 44100, 0, "NONE", ""))
        audio.writeframes(b"\x00\x00\x00\x00" * 44100 * 50)
    source.write_bytes(stereo.getvalue())
    decoder = AudioDecoder()
    assert ".mp4" in decoder.extensions
    assert decoder.duration(source) == pytest.approx(50, abs=0.1)
    chunks = list(decoder.chunks(source, 20, lambda: None))
    assert [round(c.offset) for c in chunks] == [0, 20, 40]
    with wave.open(io.BytesIO(chunks[0].wav), "rb") as audio:
        assert (audio.getframerate(), audio.getnchannels()) == (16000, 1)


# --- through the API ------------------------------------------------------------------------------------------------


def login(api_main, username):
    api_main.login_limiter.reset()
    client = TestClient(api_main.app, follow_redirects=False)
    token = re.search(r'name="csrf_token" value="([^"]+)"', client.get("/login").text).group(1)
    answer = client.post("/login", data={"username": username, "password": PASSWORDS[username], "csrf_token": token})
    assert answer.status_code == 303
    client.headers["X-CSRF-Token"] = client.get("/session").json()["csrf_token"]
    return client


@pytest.fixture
def meetings(api_main, monkeypatch):
    worker = api_main.meeting_worker
    calls: list[float] = []
    monkeypatch.setattr(worker, "voice_status", lambda: {"stt": True, "max_seconds": 120})
    monkeypatch.setattr(worker, "transcribe", fake_transcribe(calls))
    monkeypatch.setattr(worker, "llm", fake_llm)
    # Without ffmpeg the decoder reads WAV itself; the tests behave the same either way.
    monkeypatch.setattr(worker.decoder, "ffmpeg", None)
    # Each test starts with an empty queue, whatever earlier tests left behind.
    while worker.run_next():
        pass
    worker.calls = calls
    return worker


def upload(client, data: bytes, filename="heti megbeszélés.wav"):
    return client.post("/meetings/jobs", params={"filename": filename}, content=data,
                       headers={"Content-Type": "application/octet-stream"})


def test_upload_transcribes_in_chunks_summarises_and_exports(api_main, meetings):
    apa = login(api_main, "apa")
    assert apa.get("/meetings/formats").json()["video"] is False
    created = upload(apa, make_wav(100))
    assert created.status_code == 202, created.text
    job = created.json()
    assert job["status"] == "queued"
    assert meetings.source_path(job["id"]).stat().st_mode & 0o777 == 0o600

    assert meetings.run_next() is True
    assert meetings.calls == pytest.approx([45, 45, 10])
    # The recording itself is not kept once it is processed.
    assert not meetings.source_path(job["id"]).exists()

    detail = apa.get(f"/meetings/jobs/{job['id']}").json()
    assert detail["status"] == "succeeded", detail
    segments = detail["segments"]
    assert [s["start"] for s in segments] == [0.5, 5.0, 45.5, 50.0, 90.5, 95.0]
    assert segments[2]["end"] == 49.0
    assert all(s["speaker"] is None for s in segments)
    assert segments[0]["text"] == "Részlet 1: a jelentést péntekig elkészítjük."
    actions = detail["notes"]["actions"]
    assert [a["assignee"] for a in actions] == [None] and actions[0]["id"] == 1
    assert detail["notes"]["decisions"][0]["segments"] == [1]
    assert detail["notes"]["questions"][0]["segments"] == [6]

    # An assignee only when the person links a name.
    assert apa.patch(f"/meetings/jobs/{job['id']}/actions/1", json={"name": "  Kiss   Anna "}).json() == {
        "assignee": "Kiss Anna"}
    assert apa.patch(f"/meetings/jobs/{job['id']}/actions/9", json={"name": "Valaki"}).status_code == 404
    assert apa.get(f"/meetings/jobs/{job['id']}").json()["notes"]["actions"][0]["assignee"] == "Kiss Anna"

    markdown = apa.get(f"/meetings/jobs/{job['id']}/export", params={"format": "md"})
    assert markdown.status_code == 200
    assert "attachment" in markdown.headers["content-disposition"]
    assert "## Teendők\n\n- Jelentés elkészítése — Felelős: Kiss Anna (00:00:00.500)" in markdown.text
    srt = apa.get(f"/meetings/jobs/{job['id']}/export", params={"format": "srt"}).text
    assert "3\n00:00:45,500 --> 00:00:49,000\n" in srt
    txt = apa.get(f"/meetings/jobs/{job['id']}/export", params={"format": "txt"}).text
    assert txt.count("Ismeretlen beszélő") == 6

    assert apa.patch(f"/meetings/jobs/{job['id']}/actions/1", json={"name": ""}).json() == {"assignee": None}
    assert apa.delete(f"/meetings/jobs/{job['id']}").status_code == 204
    assert apa.get(f"/meetings/jobs/{job['id']}").status_code == 404


def test_meetings_are_private_to_their_owner(api_main, meetings):
    apa, anya = login(api_main, "apa"), login(api_main, "anya")
    job_id = upload(apa, make_wav(5)).json()["id"]
    meetings.run_next()
    assert job_id not in [j["id"] for j in anya.get("/meetings/jobs").json()]
    assert job_id in [j["id"] for j in apa.get("/meetings/jobs").json()]
    assert anya.get(f"/meetings/jobs/{job_id}").status_code == 404
    assert anya.get(f"/meetings/jobs/{job_id}/export", params={"format": "txt"}).status_code == 404
    assert anya.patch(f"/meetings/jobs/{job_id}/actions/1", json={"name": "Valaki"}).status_code == 404
    assert anya.post(f"/meetings/jobs/{job_id}/cancel").status_code == 404
    assert anya.delete(f"/meetings/jobs/{job_id}").status_code == 404
    assert apa.get(f"/meetings/jobs/{job_id}").json()["notes"]["actions"][0]["assignee"] is None


def test_queued_job_can_be_cancelled_and_its_upload_is_removed(api_main, meetings):
    apa = login(api_main, "apa")
    job_id = upload(apa, make_wav(5)).json()["id"]
    assert apa.post(f"/meetings/jobs/{job_id}/cancel").json() == {"status": "cancelled"}
    assert not meetings.source_path(job_id).exists()
    assert meetings.run_next() is False
    assert apa.get(f"/meetings/jobs/{job_id}").json()["status"] == "cancelled"


def test_running_job_stops_between_chunks_when_cancelled(api_main, meetings, monkeypatch):
    apa = login(api_main, "apa")
    job_id = upload(apa, make_wav(100)).json()["id"]
    transcribe = meetings.transcribe

    def cancel_after_first(wav):
        answer = transcribe(wav)
        assert apa.post(f"/meetings/jobs/{job_id}/cancel").json() == {"status": "cancelling"}
        return answer

    monkeypatch.setattr(meetings, "transcribe", cancel_after_first)
    meetings.run_next()
    assert len(meetings.calls) == 1
    assert apa.get(f"/meetings/jobs/{job_id}").json()["status"] == "cancelled"


def test_uploads_are_checked_before_queueing(api_main, meetings, monkeypatch):
    apa = login(api_main, "apa")
    assert upload(apa, b"x", filename="video.mp4").status_code == 415
    assert upload(apa, b"", filename="ures.wav").status_code == 422
    monkeypatch.setattr(sys.modules["meeting_routes"], "MAX_BYTES", 1000)
    assert upload(apa, make_wav(1)).status_code == 413
    assert list(meetings.directory.iterdir()) == []


def test_unreadable_audio_and_missing_timestamps_fail_with_a_reason(api_main, meetings, monkeypatch):
    apa = login(api_main, "apa")
    bad = upload(apa, b"not a wav file").json()["id"]
    meetings.run_next()
    assert apa.get(f"/meetings/jobs/{bad}").json()["error"] == "A fájl hangja nem olvasható."

    monkeypatch.setattr(meetings, "transcribe", lambda wav: {"text": "régi hangszolgáltatás"})
    old = upload(apa, make_wav(5)).json()["id"]
    meetings.run_next()
    assert "időbélyeg" in apa.get(f"/meetings/jobs/{old}").json()["error"]


def test_transcript_survives_when_the_model_fails(api_main, meetings, monkeypatch):
    apa = login(api_main, "apa")
    monkeypatch.setattr(meetings, "llm", lambda prompt, json_mode: "nem JSON")
    job_id = upload(apa, make_wav(10)).json()["id"]
    meetings.run_next()
    detail = apa.get(f"/meetings/jobs/{job_id}").json()
    assert detail["status"] == "failed" and "leirat letölthető" in detail["error"]
    assert detail["notes"] is None and len(detail["segments"]) == 2
    assert apa.get(f"/meetings/jobs/{job_id}/export", params={"format": "srt"}).status_code == 200
    assert apa.get(f"/meetings/jobs/{job_id}/export", params={"format": "md"}).status_code == 409
