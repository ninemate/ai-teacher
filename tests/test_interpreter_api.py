"""Tolmács routes: a turn end to end with fake stages, saved sessions isolated per user, export, latency summary."""

import base64

import pytest
from test_listen_api import login


@pytest.fixture
def rig(api_main, monkeypatch, tmp_path):
    engine = api_main.interpreter_engine
    seen = {"stt": [], "llm": [], "tts": []}

    def transcribe(content, filename, content_type, language, beam):
        seen["stt"].append((content, filename, language, beam))
        return {"text": "Jó napot", "language": "hu" if language == "auto" else language}

    def translate(prompt, model, options):
        seen["llm"].append((prompt, model, options))
        return "Good day"

    def speak(text, voice):
        seen["tts"].append((text, voice))
        return b"ID3-mp3"

    monkeypatch.setattr(engine, "_transcribe", transcribe)
    monkeypatch.setattr(engine, "_translate", translate)
    monkeypatch.setattr(engine, "_speak", speak)
    monkeypatch.setattr(engine, "_available_voices", lambda: ["hu_HU-imre-medium", "en_US-lessac-medium"])
    monkeypatch.setattr(api_main.interpreter_log, "path", tmp_path / "lat.jsonl")
    return seen


def turn_form(**extra):
    return {"side": "hu", "mode": "fast", **extra}


def test_turn_returns_translation_audio_and_latency_and_logs_it(api_main, rig):
    client, headers = login(api_main, "apa")
    response = client.post(
        "/interpreter/turn", data=turn_form(), files={"audio": ("felvetel.webm", b"opus", "audio/webm")}, headers=headers
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["source_text"] == "Jó napot" and body["translation"] == "Good day"
    assert (body["source_lang"], body["target_lang"]) == ("hu", "en")
    assert base64.b64decode(body["audio"]) == b"ID3-mp3"
    assert set(body["latency"]) == {"stt_ms", "translate_ms", "tts_ms", "total_ms"}
    assert all(v is not None for v in body["latency"].values())
    assert body["latency"]["total_ms"] >= body["latency"]["stt_ms"]
    assert rig["stt"][0][1].endswith(".webm") and rig["tts"] == [("Good day", "en_US-lessac-medium")]
    summary = client.get("/interpreter/latency", headers=headers).json()
    assert summary["fast"]["count"] >= 1 and "total_ms" in summary["fast"]["stages"]


def test_fast_and_professional_send_different_requests(api_main, rig):
    client, headers = login(api_main, "apa")
    for mode in ("fast", "professional"):
        client.post("/interpreter/turn", data=turn_form(mode=mode, text="Szia", glossary="a = b"), headers=headers)
    (prompt_f, _, opts_f), (prompt_p, _, opts_p) = rig["llm"]
    assert "a = b" not in prompt_f and "a = b" in prompt_p
    assert opts_f["num_predict"] != opts_p["num_predict"]


def test_turn_errors(api_main, rig):
    client, headers = login(api_main, "apa")
    assert client.post("/interpreter/turn", data=turn_form(mode="nope", text="x"), headers=headers).status_code == 422
    assert client.post("/interpreter/turn", data=turn_form(), headers=headers).status_code == 422
    assert client.post("/interpreter/turn", data={"side": "hu"}).status_code in (401, 403)
    empty = client.post("/interpreter/turn", data=turn_form(), files={"audio": ("a.webm", b"", "audio/webm")}, headers=headers)
    assert empty.status_code == 422


def test_config_lists_modes_and_voices(api_main, rig):
    client, _ = login(api_main, "apa")
    body = client.get("/interpreter/config").json()
    assert set(body["modes"]) == {"fast", "professional"}
    assert body["voices"] == {"hu": "hu_HU-imre-medium", "en": "en_US-lessac-medium"}


SAVED = {
    "title": "Orvosnál",
    "mode": "professional",
    "turns": [
        {"source_lang": "hu", "target_lang": "en", "source_text": "Fáj a fejem | nagyon", "translation": "I have a headache",
         "latency": {"stt_ms": 1, "translate_ms": 2, "tts_ms": 3, "total_ms": 6}},
        {"source_lang": "en", "target_lang": "hu", "source_text": "Since when?", "translation": "Mióta?"},
    ],
}


def test_sessions_are_saved_updated_exported_and_deleted(api_main, rig):
    client, headers = login(api_main, "apa")
    created = client.post("/interpreter/sessions", json=SAVED, headers=headers)
    assert created.status_code == 201
    sid = created.json()["id"]
    listing = client.get("/interpreter/sessions").json()
    assert any(item["id"] == sid and item["turns"] == 2 for item in listing)
    updated = client.put(f"/interpreter/sessions/{sid}", json={**SAVED, "title": "Új cím", "turns": SAVED["turns"][:1]}, headers=headers)
    assert updated.json()["title"] == "Új cím" and len(updated.json()["turns"]) == 1
    md = client.get(f"/interpreter/sessions/{sid}/export?format=md")
    assert md.status_code == 200 and "| Magyar | English |" in md.text and "Fáj a fejem \\| nagyon" in md.text
    assert "attachment" in md.headers["content-disposition"]
    csv_text = client.get(f"/interpreter/sessions/{sid}/export?format=csv").text
    assert csv_text.splitlines()[0].startswith("#,from,source") and "I have a headache" in csv_text
    assert "[Magyar]" in client.get(f"/interpreter/sessions/{sid}/export?format=txt").text
    assert client.get(f"/interpreter/sessions/{sid}/export?format=json").json()["title"] == "Új cím"
    assert client.delete(f"/interpreter/sessions/{sid}", headers=headers).status_code == 204
    assert client.get(f"/interpreter/sessions/{sid}").status_code == 404


def test_sessions_and_latency_are_isolated_between_users(api_main, rig):
    apa, apa_headers = login(api_main, "apa")
    anya, anya_headers = login(api_main, "anya")
    sid = apa.post("/interpreter/sessions", json=SAVED, headers=apa_headers).json()["id"]
    apa.post("/interpreter/turn", data=turn_form(text="Szia"), headers=apa_headers)
    assert all(item["id"] != sid for item in anya.get("/interpreter/sessions").json())
    assert anya.get(f"/interpreter/sessions/{sid}").status_code == 404
    assert anya.get(f"/interpreter/sessions/{sid}/export").status_code == 404
    assert anya.put(f"/interpreter/sessions/{sid}", json=SAVED, headers=anya_headers).status_code == 404
    assert anya.delete(f"/interpreter/sessions/{sid}", headers=anya_headers).status_code == 404
    assert apa.get(f"/interpreter/sessions/{sid}").status_code == 200, "still there"
    assert anya.get("/interpreter/latency", headers=anya_headers).json() == {}
    assert anya.get("/interpreter/latency?scope=all").status_code == 403


def test_session_validation(api_main, rig):
    client, headers = login(api_main, "apa")
    bad = {**SAVED, "turns": [{**SAVED["turns"][0], "source_lang": "de"}]}
    assert client.post("/interpreter/sessions", json=bad, headers=headers).status_code == 422
    assert client.post("/interpreter/sessions", json=SAVED).status_code in (401, 403), "CSRF protected"
