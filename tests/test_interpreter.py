"""Tolmács: the STT -> translation -> TTS chain, its latency bookkeeping and the mode differences, all with fakes."""

import json
from pathlib import Path

import pytest
from teacher_common import interpreter
from teacher_common.interpreter import Interpreter, InterpreterError, LatencyLog

ROOT = Path(__file__).resolve().parents[1]


class Clock:
    """A fake clock the fake stages advance, so the measured figures are exact."""

    def __init__(self):
        self.now = 100.0

    def perf_counter(self):
        return self.now

    def strftime(self, fmt):
        return "2026-01-01T00:00:00+0000"


class Fakes:
    def __init__(self, clock, heard=("Jó napot kívánok", "hu")):
        self.clock, self.heard, self.calls = clock, heard, []
        self.stt_s, self.llm_s, self.tts_s = 1.5, 0.75, 0.25
        self.tts_fails = False

    def transcribe(self, content, filename, content_type, language, beam):
        self.calls.append(("stt", language, beam))
        self.clock.now += self.stt_s
        return {"text": self.heard[0], "language": self.heard[1]}

    def translate(self, prompt, model, options):
        self.calls.append(("llm", model, options, prompt))
        self.clock.now += self.llm_s
        return '"Good afternoon"'

    def speak(self, text, voice):
        self.calls.append(("tts", text, voice))
        if self.tts_fails:
            raise RuntimeError("voice down")
        self.clock.now += self.tts_s
        return b"mp3"


@pytest.fixture
def clock(monkeypatch):
    fake = Clock()
    monkeypatch.setattr(interpreter, "time", fake)
    return fake


def make(clock, config=None, **kwargs):
    fakes = Fakes(clock, **kwargs)
    config = config or interpreter.load_config(str(ROOT / "config" / "models" / "interpreter.json"))
    engine = Interpreter(
        config, ROOT / "config" / "prompts", fakes.transcribe, fakes.translate, fakes.speak,
        available_voices=lambda: ["hu_HU-imre-medium", "en_US-lessac-medium"],
    )
    return engine, fakes


AUDIO = (b"opus", "felvetel.webm", "audio/webm")


def test_chain_runs_in_order_and_reports_each_stage(clock):
    engine, fakes = make(clock)
    turn = engine.run("fast", "hu", audio=AUDIO)
    assert [c[0] for c in fakes.calls] == ["stt", "llm", "tts"]
    assert (turn.source_lang, turn.target_lang) == ("hu", "en")
    assert turn.source_text == "Jó napot kívánok"
    assert turn.translation == "Good afternoon", "wrapping quotes are stripped"
    assert turn.audio == b"mp3"
    assert turn.latency.as_dict() == {"stt_ms": 1500.0, "translate_ms": 750.0, "tts_ms": 250.0, "total_ms": 2500.0}
    # TTS speaks the translation with an installed voice of the target language
    assert fakes.calls[2][1:] == ("Good afternoon", "en_US-lessac-medium")


def test_english_side_goes_to_hungarian_voice(clock):
    engine, fakes = make(clock, heard=("Good morning", "en"))
    turn = engine.run("fast", "en", audio=AUDIO)
    assert (turn.source_lang, turn.target_lang) == ("en", "hu")
    assert fakes.calls[2][2] == "hu_HU-imre-medium"
    assert "angol szöveget magyarra" in fakes.calls[1][3]


def test_auto_uses_the_detected_language_and_rejects_others(clock):
    engine, fakes = make(clock, heard=("Good morning", "en"))
    assert engine.run("fast", "auto", audio=AUDIO).target_lang == "hu"
    assert fakes.calls[0][1] == "auto"
    engine, _ = make(clock, heard=("Guten Morgen", "de"))
    with pytest.raises(InterpreterError, match="magyar és angol"):
        engine.run("fast", "auto", audio=AUDIO)
    with pytest.raises(InterpreterError):
        engine.run("fast", "auto", text="no audio, nothing to detect from")


def test_text_input_skips_stt(clock):
    engine, fakes = make(clock)
    turn = engine.run("fast", "hu", text="Szia")
    assert [c[0] for c in fakes.calls] == ["llm", "tts"]
    assert turn.latency.stt_ms is None and turn.latency.total_ms == 1000.0


def test_tts_failure_keeps_the_translation_and_is_reported(clock):
    engine, fakes = make(clock)
    fakes.tts_fails = True
    turn = engine.run("fast", "hu", audio=AUDIO)
    assert turn.translation == "Good afternoon" and turn.audio is None and turn.tts_error
    assert turn.latency.tts_ms is not None


def test_no_speech_is_an_error_before_translation(clock):
    engine, fakes = make(clock, heard=("  ", "hu"))
    with pytest.raises(InterpreterError, match="Nem hallottam"):
        engine.run("fast", "hu", audio=AUDIO)
    assert [c[0] for c in fakes.calls] == ["stt"]


def test_modes_differ_in_prompt_model_options_and_glossary(clock):
    config = interpreter.load_config(str(ROOT / "config" / "models" / "interpreter.json"))
    config["modes"]["fast"]["model"] = "small-model"
    config["modes"]["professional"]["model"] = "big-model"
    engine, fakes = make(clock, config=config)
    glossary = "kuratórium = board of trustees\n"
    engine.run("fast", "hu", audio=AUDIO, glossary=glossary)
    engine.run("professional", "hu", audio=AUDIO, glossary=glossary)
    (_, _, beam_f), (_, _, beam_p) = [c for c in fakes.calls if c[0] == "stt"]
    (_, model_f, opts_f, prompt_f), (_, model_p, opts_p, prompt_p) = [c for c in fakes.calls if c[0] == "llm"]
    assert (model_f, model_p) == ("small-model", "big-model")
    assert beam_f < beam_p
    assert opts_f["num_predict"] < opts_p["num_predict"] and opts_f["num_ctx"] < opts_p["num_ctx"]
    assert "kuratórium" not in prompt_f and "kuratórium = board of trustees" in prompt_p
    assert len(prompt_f) < len(prompt_p) / 2, "the fast prompt is the short one"
    assert "{glossary}" not in prompt_p and "{text}" not in prompt_p


def test_glossary_is_data_not_a_template_and_is_bounded():
    assert interpreter.normalize_glossary("a = b\n\n  \nc = d") == "a = b\nc = d"
    assert len(interpreter.normalize_glossary("x = y\n" * 5000)) <= interpreter.MAX_GLOSSARY_CHARS
    mode = {"prompt_prefix": "interpreter_pro", "use_glossary": True}
    prompt = interpreter.build_prompt(ROOT / "config" / "prompts", mode, "hu", "en", "{text} {glossary}", "{x} = y")
    assert "{text} {glossary}" in prompt and "{x} = y" in prompt


def test_every_mode_and_direction_has_a_prompt():
    config = interpreter.load_config(str(ROOT / "config" / "models" / "interpreter.json"))
    for mode in config["modes"].values():
        for source, target in (("hu", "en"), ("en", "hu")):
            text = (ROOT / "config" / "prompts" / f"{mode['prompt_prefix']}_{source}_{target}.md").read_text()
            assert "{text}" in text


def test_config_falls_back_to_defaults_on_missing_or_broken_file(tmp_path):
    assert set(interpreter.load_config(str(tmp_path / "none.json"))["modes"]) == {"fast", "professional"}
    broken = tmp_path / "broken.json"
    broken.write_text("{not json")
    assert interpreter.load_config(str(broken))["modes"]["fast"]["model"] == ""
    partial = tmp_path / "partial.json"
    partial.write_text(json.dumps({"modes": {"fast": {"model": "tiny"}}}))
    cfg = interpreter.load_config(str(partial))
    assert cfg["modes"]["fast"]["model"] == "tiny" and cfg["modes"]["fast"]["num_predict"] == 256


def test_log_has_figures_but_no_text_and_summary_has_p50_p95(clock, tmp_path):
    engine, fakes = make(clock)
    log = LatencyLog(str(tmp_path / "logs" / "lat.jsonl"))
    for i in range(1, 21):  # translation takes 1..20 s
        fakes.llm_s = float(i)
        log.record("apa", engine.run("fast", "hu", audio=AUDIO), audio_seconds=2.0)
    engine.run("professional", "hu", audio=AUDIO)
    raw = log.path.read_text()
    assert "Jó napot" not in raw and "Good afternoon" not in raw
    entry = json.loads(raw.splitlines()[0])
    assert entry["mode"] == "fast" and entry["direction"] == "hu-en" and entry["translate_ms"] == 1000.0
    summary = interpreter.summarise(log.read())
    fast = summary["fast"]
    assert fast["count"] == 20
    assert fast["stages"]["translate_ms"] == {"n": 20, "p50": 10000.0, "p95": 19000.0, "max": 20000.0}
    assert fast["stages"]["stt_ms"]["p50"] == 1500.0
    assert log.read("anya") == []


def test_summary_ignores_stages_that_did_not_run_and_bad_lines(tmp_path):
    path = tmp_path / "l.jsonl"
    path.write_text(
        json.dumps({"mode": "fast", "user": "a", "stt_ms": None, "translate_ms": 10, "tts_ms": None, "total_ms": 10})
        + "\nnot json\n"
    )
    stages = interpreter.summarise(LatencyLog(str(path)).read())["fast"]["stages"]
    assert set(stages) == {"translate_ms", "total_ms"}


def test_latency_cli_prints_table_and_json(tmp_path, capsys):
    import importlib.util

    spec = importlib.util.spec_from_file_location("interpreter_latency_cli", ROOT / "scripts" / "interpreter_latency.py")
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    path = tmp_path / "l.jsonl"
    path.write_text(json.dumps({"mode": "fast", "user": "a", "translate_ms": 10, "total_ms": 20}) + "\n")
    assert cli.main([str(path)]) == 0
    assert "p95" in capsys.readouterr().out
    assert cli.main([str(path), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["fast"]["count"] == 1
    assert cli.main([str(tmp_path / "missing")]) == 2
