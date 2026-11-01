"""scripts/eval_models.py against a fake Ollama: prompts, checks, metrics, report and rating totals."""

import importlib.util
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("eval_models", ROOT / "scripts" / "eval_models.py")
ev = importlib.util.module_from_spec(spec)
sys.modules["eval_models"] = ev
spec.loader.exec_module(ev)

GOOD_QUIZ = {
    "questions": [
        {
            "question": f"Mekkora az átfogó a {n}. példában?",
            "options": ["3", "4", "5", "6"],
            "correct_index": 2,
            "explanation": "Mert 9 + 16 = 25.",
            "sources": [1],
        }
        for n in (1, 2, 3)
    ]
}
GOOD_CARDS = {"cards": [{"front": f"Fogalom {n}", "back": "Meghatározás", "sources": [1]} for n in (1, 2, 3)]}
HU_ANSWER = "A mohácsi csata 1526-ban volt, és a király is elesett a csatában, ahogy a forrás szerint [1] írja."
REFUSAL = "A megadott forrásban nincs elég információ erről, ezért nem tudom megmondani."


class FakeOllama(BaseHTTPRequestHandler):
    models = {"fast:1b", "slow:7b"}
    requests: list = []
    loaded = {"name": "fast:1b", "size": 4_000_000_000, "size_vram": 4_000_000_000}

    def log_message(self, *args):
        pass

    def _send(self, lines):
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.end_headers()
        for line in lines:
            self.wfile.write((json.dumps(line) + "\n").encode())
            self.wfile.flush()

    def do_GET(self):
        if self.path == "/api/tags":
            self._send([{"models": [{"name": m} for m in sorted(self.models)]}])
        elif self.path == "/api/ps":
            self._send([{"models": [self.loaded]}])
        else:
            self.send_error(404)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).requests.append(body)
        if body.get("keep_alive") == 0:
            return self._send([{"done": True}])
        if body.get("format") == "json":
            text = json.dumps(GOOD_CARDS if "kártyák" in body["prompt"] else GOOD_QUIZ)
        elif "Hány katonából" in body["prompt"]:
            text = REFUSAL
        else:
            text = HU_ANSWER
        half = len(text) // 2
        self._send(
            [
                {"response": text[:half], "done": False},
                {"response": text[half:], "done": False},
                {
                    "response": "",
                    "done": True,
                    "eval_count": 50,
                    "eval_duration": 2_000_000_000,
                    "load_duration": 1_500_000_000,
                    "prompt_eval_count": 800,
                },
            ]
        )


@pytest.fixture
def fake():
    FakeOllama.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeOllama)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


# --- checks ------------------------------------------------------------------------------------


def test_language_detection():
    assert ev.looks_hungarian(HU_ANSWER)
    assert not ev.looks_hungarian("The battle took place in 1526 and the king of Hungary died in the fighting there.")
    assert not ev.looks_hungarian("Rövid.")


def test_strip_thinking_removes_reasoning_even_if_unterminated():
    assert ev.strip_thinking("<think>gondolkodom</think>\nVálasz [1]") == "Válasz [1]"
    assert ev.strip_thinking("<think>soha nem ér véget") == ""


def test_quiz_json_check_uses_the_study_parser():
    ok, _ = ev.check_json(json.dumps(GOOD_QUIZ), "quiz", 2, 3)
    assert ok
    ok, note = ev.check_json("Íme a kvíz: nem JSON", "quiz", 2, 3)
    assert not ok and "JSON" in note
    ok, note = ev.check_json(json.dumps(GOOD_QUIZ), "quiz", 2, 5)
    assert not ok and "5" in note
    bad = {"questions": [{**GOOD_QUIZ["questions"][0], "correct_index": 9}]}
    assert not ev.check_json(json.dumps(bad), "quiz", 2, 1)[0]
    assert ev.check_json("```json\n" + json.dumps(GOOD_CARDS) + "\n```", "cards", 1, 3)[0]


def test_citation_and_refusal_checks():
    q = {"question": "Kérdés?", "sources": [{"text": "Az év 1526 volt."}], "expect": {"cite": True}}
    assert run_ok(q, HU_ANSWER, "forráshivatkozás")
    assert not run_ok(q, HU_ANSWER.replace("[1]", "[7]"), "forráshivatkozás")  # invented number
    refuse = {"question": "Hány katona?", "sources": [{"text": "Az év 1526 volt."}], "expect": {"refuse": True}}
    assert run_ok(refuse, REFUSAL, "elutasít, ha nincs a forrásban")
    assert not run_ok(refuse, "A sereg 25000 katonából állt [1].", "elutasít, ha nincs a forrásban")
    assert not run_ok(refuse, "Nem találtam, de 25000 katona volt.", "elutasít, ha nincs a forrásban")


def run_ok(question, text, name):
    return ev.run_checks(question, text, 1)[name]["ok"]


def test_uncertain_source_check():
    q = {"question": "Q?", "sources": [], "expect": {"uncertain": True}}
    assert run_ok(q, "Ez a forrás bizonytalan, a szkennelés rossz, a hosszát tehát ellenőrizd [1].", "jelzi a bizonytalan forrást")
    assert not run_ok(q, "A Duna hossza a forrás szerint 2850 km, ami egy elég hosszú folyó [1].", "jelzi a bizonytalan forrást")


# --- prompts ----------------------------------------------------------------------------------------


def test_prompt_is_built_like_the_api_builds_it():
    questions = ev.load_questions([ev.DEFAULT_QUESTIONS])
    by_id = {q["id"]: q for q in questions}
    prompt, json_mode, shown = ev.build_prompt(by_id["qa-mohacs"], ev.DEFAULT_PROMPT_DIR)
    assert not json_mode and shown == 1
    assert (ev.DEFAULT_PROMPT_DIR / "teacher_hu.md").read_text(encoding="utf-8").strip() in prompt
    assert ev.citations.CITATION_RULES in prompt
    assert "[1] Történelem 8. – A középkor vége" in prompt and prompt.endswith("Válasz:")
    ocr, _, _ = ev.build_prompt(by_id["ocr-duna"], ev.DEFAULT_PROMPT_DIR)
    assert "BIZONYTALAN: OCR, 52% bizonyosság" in ocr
    quiz, json_mode, _ = ev.build_prompt(by_id["quiz-pitagorasz"], ev.DEFAULT_PROMPT_DIR)
    assert json_mode and "Pontosan 3 kérdést" in quiz and "{context}" not in quiz


def test_builtin_questions_cover_the_required_kinds():
    questions = ev.load_questions([ev.DEFAULT_QUESTIONS])
    tasks = {q.get("task", "answer") for q in questions}
    assert tasks == {"answer", "summary", "quiz", "flashcards"}
    expects = [q["expect"] for q in questions]
    assert any(e.get("refuse") for e in expects) and any(e.get("cite") for e in expects)
    assert any(q["sources"][0].get("ocr") for q in questions if q.get("sources"))


def test_duplicate_question_ids_are_rejected(tmp_path):
    extra = tmp_path / "q.private.json"
    extra.write_text(json.dumps([{"id": "qa-mohacs", "question": "x"}]), encoding="utf-8")
    with pytest.raises(SystemExit):
        ev.load_questions([ev.DEFAULT_QUESTIONS, extra])


# --- GPU and placement --------------------------------------------------------------------------------


def test_nvidia_smi_parsing_and_delta():
    gpus = ev.read_gpus(lambda cmd: "0, 500, 8192\n1, 300, 8192\n")
    assert gpus == {0: {"used_mib": 500, "total_mib": 8192}, 1: {"used_mib": 300, "total_mib": 8192}}
    assert ev.vram_delta(gpus, {0: 5400, 1: 350}) == {"0": 4900}  # GPU 1 barely moved: not used
    assert ev.read_gpus(lambda cmd: "garbage") is None
    assert ev.vram_delta(None, {0: 1}) == {}


def test_placement_classification():
    assert ev.classify_placement({"size": 100, "size_vram": 0}, 0)["cpu_fallback"] is True
    split = ev.classify_placement({"size": 100, "size_vram": 60}, 1)
    assert split["placement"] == "gpu+cpu" and split["cpu_fallback"] and split["gpu_fraction"] == 0.6
    assert ev.classify_placement({"size": 100, "size_vram": 100}, 2)["placement"] == "multi-gpu"
    assert ev.classify_placement({"size": 100, "size_vram": 100}, 1)["placement"] == "single-gpu"
    assert ev.classify_placement(None, 0)["placement"] == "unknown"


def test_parse_generation_measures_ttft_and_speed():
    lines = [
        json.dumps({"response": "Sz", "done": False}),
        json.dumps({"response": "ia", "done": True, "eval_count": 30, "eval_duration": 1_000_000_000, "load_duration": 2_000_000_000}),
    ]
    run = ev.parse_generation(iter(lines), ev.time.monotonic())
    assert run["text"] == "Szia" and run["tokens_per_s"] == 30.0 and run["load_s"] == 2.0
    assert run["ttft_s"] is not None and run["error"] is None
    assert ev.parse_generation(iter([json.dumps({"error": "x"})]), ev.time.monotonic())["error"] == "x"
    assert ev.parse_generation(iter([]), ev.time.monotonic())["error"]


# --- end to end ------------------------------------------------------------------------------------------


def run_cli(url, tmp_path, *extra):
    candidates = tmp_path / "cand.json"
    candidates.write_text(
        json.dumps({"candidates": [{"tag": "fast:1b"}, {"tag": "slow:7b"}, {"tag": "absent:3b"}]}), encoding="utf-8"
    )
    return ev.main(["run", "--candidates", str(candidates), "--url", url, "--out", str(tmp_path / "out"), "--no-gpu", *extra])


def test_end_to_end_against_the_fake(fake, tmp_path, capsys):
    assert run_cli(fake, tmp_path) == 0
    out = capsys.readouterr().out
    assert "absent:3b" in out and "scripts/model-update.sh absent:3b" in out  # missing model: named, not pulled
    (run_dir,) = (tmp_path / "out").iterdir()
    results = json.loads((run_dir / "results.json").read_text(encoding="utf-8"))
    assert [m["tag"] for m in results["models"]] == ["fast:1b", "slow:7b"]
    fast = results["models"][0]
    assert len(fast["answers"]) == 7 and fast["summary"]["errors"] == 0
    assert fast["answers"][0]["tokens_per_s"] == 25.0 and fast["answers"][0]["ttft_s"] is not None
    assert fast["placement"] == "single-gpu" and fast["cpu_fallback"] is False
    # Every check the fake can satisfy passes, the OCR one fails (the fake never says "bizonytalan").
    failed = [(a["question_id"], n) for a in fast["answers"] for n, c in a["checks"].items() if not c["ok"]]
    assert set(failed) == {("ocr-duna", "jelzi a bizonytalan forrást"), ("ocr-duna", "tartalmazza a várt tényt")}
    # The real teacher prompt was sent, and the api's generation options.
    first = FakeOllama.requests[0]
    assert "Te egy magyar tanársegéd vagy." in first["prompt"] and first["options"]["num_ctx"] == 8192
    assert any(r.get("format") == "json" for r in FakeOllama.requests)
    assert any(r.get("keep_alive") == 0 for r in FakeOllama.requests)  # models are unloaded between candidates
    report = (run_dir / "report.md").read_text(encoding="utf-8")
    assert report.count("**Értékelés (1-5):**") == 14 and "Modell A" in report and "fast:1b" not in report


def test_cpu_fallback_is_reported(fake, tmp_path):
    FakeOllama.loaded = {"name": "fast:1b", "size": 4_000_000_000, "size_vram": 0}
    try:
        assert run_cli(fake, tmp_path, "--only", "fast", "--no-blind") == 0
    finally:
        FakeOllama.loaded = {"name": "fast:1b", "size": 4_000_000_000, "size_vram": 4_000_000_000}
    (run_dir,) = (tmp_path / "out").iterdir()
    model = json.loads((run_dir / "results.json").read_text(encoding="utf-8"))["models"][0]
    assert model["placement"] == "cpu" and model["cpu_fallback"] is True
    assert "CPU-ra esett vissza" in (run_dir / "report.md").read_text(encoding="utf-8")


def test_ratings_are_totalled_through_the_blind_labels(fake, tmp_path, capsys):
    run_cli(fake, tmp_path)
    (run_dir,) = (tmp_path / "out").iterdir()
    results = json.loads((run_dir / "results.json").read_text(encoding="utf-8"))
    report_path = run_dir / "report.md"
    labels = results["labels"]
    lines, current, ratings = [], None, {"fast:1b": iter([5, 4, 5]), "slow:7b": iter([2, 3])}
    by_label = {v: k for k, v in labels.items()}
    for line in report_path.read_text(encoding="utf-8").splitlines():
        if line.startswith("#### "):
            current = by_label[line[5:]]
        if line.startswith("**Értékelés (1-5):**"):
            value = next(ratings[current], None)
            line = line + (f" {value}" if value else "")
        lines.append(line)
    report_path.write_text("\n".join(lines), encoding="utf-8")
    capsys.readouterr()
    totals = ev.total_ratings(report_path.read_text(encoding="utf-8"), results)
    assert totals["fast:1b"] == {"sum": 14, "count": 3, "mean": 4.67}
    assert totals["slow:7b"] == {"sum": 5, "count": 2, "mean": 2.5}
    assert ev.main(["totals", str(report_path)]) == 0
    assert "fast:1b" in capsys.readouterr().out


def test_unreachable_ollama_is_a_clear_error(tmp_path):
    with pytest.raises(SystemExit, match="cannot reach Ollama"):
        run_cli("http://127.0.0.1:9", tmp_path)


# --- privacy guards -------------------------------------------------------------------------------------------


def test_results_inside_the_repo_must_be_gitignored():
    ev.ensure_ignored(ROOT / "eval-results")  # the default place is ignored
    ev.ensure_ignored(Path("/tmp/anywhere-outside-the-repo"))
    with pytest.raises(SystemExit):
        ev.ensure_ignored(ROOT / "docs")


def test_private_and_results_paths_are_gitignored():
    for name in ("eval-results/run/results.json", "mine.private.json", "eval-private/q.json"):
        assert subprocess.run(["git", "-C", str(ROOT), "check-ignore", "-q", name]).returncode == 0, name


# --- the relay that runs in the api container ------------------------------------------------------------------


def test_relay_forwards_the_stream_line_by_line(fake):
    request = json.dumps({"method": "POST", "path": "/api/generate", "body": {"model": "fast:1b", "prompt": "Szia"}})
    proc = subprocess.run(
        [sys.executable, "-I", "-c", ev.RELAY],
        input=request, capture_output=True, text=True, env={**os.environ, "OLLAMA_BASE_URL": fake}, check=True,
    )  # fmt: skip
    chunks = [json.loads(line) for line in proc.stdout.splitlines()]
    assert chunks[-1]["done"] is True and len(chunks) == 3


def test_every_placement_has_a_report_wording():
    for placement, fraction in (("cpu", 0.0), ("gpu+cpu", 0.6), ("multi-gpu", 1.0), ("single-gpu", 1.0), ("unknown", None)):
        assert ev.describe_placement({"placement": placement, "gpu_fraction": fraction})
    assert "60%" in ev.describe_placement({"placement": "gpu+cpu", "gpu_fraction": 0.6})
