#!/usr/bin/env python3
"""Compare chat model candidates on Hungarian teaching tasks, through the Ollama of this stack.

Runs every candidate in a config file against a question set with the real teacher prompt, measures
speed and GPU use, applies the automatic checks that are honest for a script, and writes a results file
plus a side-by-side Markdown report with rating fields to fill in. See docs/model-selection.md.

    python3 scripts/eval_models.py run --candidates config/models/eval_candidates.example.json
    python3 scripts/eval_models.py totals eval-results/<run>/report.md

The private services publish no ports and have no egress (docs/security.md), so by default the requests
go through the stack's own `api` container (`docker compose exec`), which already sits on the private
network next to Ollama. Nothing is published and nothing is pulled; a missing model is reported with the
`scripts/model-update.sh` command that fetches it. `--url` talks to an Ollama directly instead (a local
install, or the test double in tests/test_eval_models.py).
"""

from __future__ import annotations

import argparse
import json
import random
import re
import shutil
import statistics
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "common"))

from teacher_common import citations  # noqa: E402  (stdlib only)

DEFAULT_QUESTIONS = ROOT / "config" / "eval" / "questions_hu.json"
DEFAULT_PROMPT_DIR = ROOT / "config" / "prompts"
DEFAULT_OUT = ROOT / "eval-results"
# The same generation settings the api uses (services/api/app/main.py call_ollama).
DEFAULT_OPTIONS = {"temperature": 0.2, "num_ctx": 8192}
# Same limits as the api defaults (RETRIEVAL_WEAK_SCORE, OCR_LOW_CONFIDENCE, MAX_CONTEXT_CHARS).
LIMITS = citations.Limits(weak_score=0.8, ocr_min_confidence=70)
MAX_CONTEXT_CHARS = 6000
TASK_INSTRUCTION = {
    "answer": "Válaszold meg a kérdést tanári stílusban.",
    "summary": "Készíts tömör összefoglalót a témáról.",
}

# --- transports -----------------------------------------------------------------------------------
# A transport is a callable (method, path, body) -> iterator of response lines (str).

# Runs inside the api container: forwards one request to the Ollama of the private network and relays the
# response line by line, flushed, so the host can time the first token. Stdlib only (urllib).
RELAY = r"""
import json, os, sys, urllib.request, urllib.error
req = json.load(sys.stdin)
base = os.environ.get("OLLAMA_BASE_URL", "http://ollama:11434").rstrip("/")
data = json.dumps(req["body"]).encode() if req.get("body") is not None else None
r = urllib.request.Request(base + req["path"], data=data, method=req["method"],
                           headers={"Content-Type": "application/json"})
try:
    with urllib.request.urlopen(r, timeout=1800) as resp:
        for line in resp:
            sys.stdout.write(line.decode("utf-8", "replace").rstrip("\n") + "\n")
            sys.stdout.flush()
except urllib.error.HTTPError as exc:
    print(json.dumps({"error": exc.read().decode("utf-8", "replace")[:500] or str(exc), "status": exc.code}))
except Exception as exc:
    print(json.dumps({"error": str(exc)}))
"""


def compose_transport(env_file: Path | None = None, compose_file: Path | None = None) -> Callable:
    env_file = env_file or (ROOT / ".env" if (ROOT / ".env").exists() else ROOT / ".env.example")
    compose_file = compose_file or ROOT / "compose" / "docker-compose.yml"
    command = [
        "docker", "compose", "--env-file", str(env_file), "-f", str(compose_file),
        "exec", "-T", "api", "python", "-c", RELAY,
    ]  # fmt: skip

    def transport(method: str, path: str, body: dict | None) -> Iterator[str]:
        proc = subprocess.Popen(  # noqa: S603
            command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        assert proc.stdin and proc.stdout and proc.stderr
        proc.stdin.write(json.dumps({"method": method, "path": path, "body": body}))
        proc.stdin.close()
        yield from (line.rstrip("\n") for line in proc.stdout)
        code = proc.wait()
        if code:
            error = proc.stderr.read().strip()
            yield json.dumps({"error": f"docker compose exec failed ({code}): {error[:300]}"})

    return transport


def url_transport(base_url: str) -> Callable:
    base = base_url.rstrip("/")

    def transport(method: str, path: str, body: dict | None) -> Iterator[str]:
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(  # noqa: S310
            base + path, data=data, method=method, headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(request, timeout=1800) as response:  # noqa: S310
                for line in response:
                    yield line.decode("utf-8", "replace").rstrip("\n")
        except urllib.error.HTTPError as exc:
            yield json.dumps({"error": exc.read().decode("utf-8", "replace")[:500] or str(exc), "status": exc.code})
        except (urllib.error.URLError, OSError) as exc:
            yield json.dumps({"error": str(exc)})

    return transport


def call_json(transport: Callable, method: str, path: str, body: dict | None = None) -> dict:
    """One non-streaming request; returns {"error": ...} when the call failed."""
    for line in transport(method, path, body):
        if line.strip():
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                return {"error": f"not JSON: {line[:200]}"}
    return {"error": "empty response"}


# --- GPU --------------------------------------------------------------------------------------------


def read_gpus(runner: Callable[[list[str]], str] | None = None) -> dict[int, dict] | None:
    """{index: {used_mib, total_mib}} from nvidia-smi, or None when it is not available."""
    if runner is None:
        if not shutil.which("nvidia-smi"):
            return None

        def runner(cmd: list[str]) -> str:
            return subprocess.run(cmd, capture_output=True, text=True, timeout=15, check=True).stdout  # noqa: S603

    try:
        out = runner(["nvidia-smi", "--query-gpu=index,memory.used,memory.total", "--format=csv,noheader,nounits"])
    except (subprocess.SubprocessError, OSError):
        return None
    gpus: dict[int, dict] = {}
    for line in out.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) == 3 and all(p.isdigit() for p in parts):
            gpus[int(parts[0])] = {"used_mib": int(parts[1]), "total_mib": int(parts[2])}
    return gpus or None


class GpuSampler:
    """Polls per-GPU memory while a request runs; keeps the peak per GPU."""

    def __init__(self, read: Callable[[], dict[int, dict] | None], interval: float = 0.5):
        self.read, self.interval = read, interval
        self.peak: dict[int, int] = {}
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def _sample(self) -> None:
        for index, info in (self.read() or {}).items():
            self.peak[index] = max(self.peak.get(index, 0), info["used_mib"])

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._sample()
            self._stop.wait(self.interval)

    def __enter__(self) -> GpuSampler:
        self._sample()
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
        self._thread.join()
        self._sample()


def vram_delta(baseline: dict[int, dict] | None, peak: dict[int, int]) -> dict[str, int]:
    """MiB added per GPU over the idle baseline; GPUs the model did not touch (< 100 MiB) are left out."""
    if not baseline:
        return {}
    delta = {str(i): peak[i] - baseline[i]["used_mib"] for i in peak if i in baseline}
    return {i: mib for i, mib in delta.items() if mib >= 100}


def classify_placement(ps_entry: dict | None, gpus_used: int) -> dict:
    """Where Ollama put the model, from /api/ps (size = total bytes, size_vram = bytes on GPUs)."""
    if not ps_entry or not ps_entry.get("size"):
        return {"placement": "unknown", "gpu_fraction": None, "cpu_fallback": None}
    fraction = ps_entry.get("size_vram", 0) / ps_entry["size"]
    if fraction <= 0.01:
        placement = "cpu"
    elif fraction < 0.99:
        placement = "gpu+cpu"
    elif gpus_used > 1:
        placement = "multi-gpu"
    else:
        placement = "single-gpu"
    return {"placement": placement, "gpu_fraction": round(fraction, 3), "cpu_fallback": fraction < 0.99}


# --- prompts -------------------------------------------------------------------------------------------


def _hit(source: dict, number: int) -> dict:
    return {
        "chunk_id": f"eval-{number}",
        "title": source.get("title"),
        "locator": source.get("locator"),
        "text": source.get("text", ""),
        "score": source.get("score", 0.9),
        "ocr": bool(source.get("ocr")),
        "ocr_confidence": source.get("ocr_confidence"),
    }


def build_prompt(question: dict, prompt_dir: Path, teacher_prompt: Path | None = None) -> tuple[str, bool, int]:
    """(prompt, json_mode, number of sources shown), built like the api builds it for the same task."""
    task = question.get("task", "answer")
    hits = [_hit(s, n) for n, s in enumerate(question.get("sources", []), start=1)]
    level = question.get("level", "középhaladó")
    if task in ("quiz", "flashcards"):
        from teacher_common import study  # needs pydantic, like the api

        context, used = study.build_context(hits, MAX_CONTEXT_CHARS)
        template = study.load_prompt(prompt_dir, task)
        prompt = study.render_prompt(
            template, level=level, count=question.get("count", 3), topic=question["question"], context=context
        )
        return prompt, True, len(used)
    built = citations.build_citations(hits, LIMITS) if hits else []
    context, used = citations.build_context(built, hits, MAX_CONTEXT_CHARS)
    base = (teacher_prompt or prompt_dir / "teacher_hu.md").read_text(encoding="utf-8").strip()
    prompt = (
        f"{base}\n\n"
        f"Feladat: {TASK_INSTRUCTION[task]}\n"
        f"Nehézségi szint: {level}\n\n"
        f"{citations.CITATION_RULES}\n\n"
        f"Forrásrészletek:\n{context}\n\n"
        f"Felhasználói kérés: {question['question']}\n\n"
        "Válasz:"
    )
    return prompt, False, len(used)


# --- automatic checks ----------------------------------------------------------------------------------
# Only what a script can judge honestly. Language quality is left to the human ratings.

HU_WORDS = frozenset(
    "a az és hogy nem egy is de van meg ez azt ami mint vagy volt lehet még csak kell vagyis mert során "
    "által szerint után között alatt ezért akkor ahol amely amelyek ebben ennek annak olyan nagy első "
    "két tehát például forrás".split()
)
EN_WORDS = frozenset("the and of is to in that it for with as are this be on by from or an which was".split())
REFUSAL_PHRASES = (
    "nincs elég információ", "nem találtam", "nem szerepel", "nem derül ki", "nem található", "nem tartalmaz",
    "nem olvasható", "nem ismert", "nincs információ", "nem említ", "nem áll rendelkezésre", "nem tudom",
    "nincs adat", "nem lehet megválaszolni", "nem válaszolható",
)  # fmt: skip
THINK_BLOCK = re.compile(r"<think>.*?(</think>|$)", re.DOTALL | re.IGNORECASE)


def strip_thinking(text: str) -> str:
    """Reasoning models emit <think>…</think> first; the answer is what follows."""
    return THINK_BLOCK.sub("", text).strip()


def looks_hungarian(text: str) -> bool:
    words = re.findall(r"[^\W\d_]+", text.lower())
    if len(words) < 8:
        return False
    hu = sum(w in HU_WORDS for w in words) / len(words)
    en = sum(w in EN_WORDS for w in words) / len(words)
    accents = sum(c in "áéíóöőúüű" for c in text.lower()) / max(len(text), 1)
    return hu >= 0.08 and hu > 1.5 * en and accents > 0.02


def check_json(text: str, kind: str, source_count: int, count: int | None) -> tuple[bool, str]:
    from teacher_common import study

    parser = study.parse_quiz if kind == "quiz" else study.parse_flashcards
    hits = [{"chunk_id": f"eval-{n}"} for n in range(1, source_count + 1)]
    try:
        result = parser(text, hits)
    except ValueError as exc:
        return False, f"nem érvényes JSON / szerkezet: {exc}"
    if result.rejected:
        return False, f"{len(result.rejected)} elem érvénytelen: {'; '.join(result.rejected[:2])}"
    if not result.items:
        return False, "üres lista"
    if count and len(result.items) != count:
        return False, f"{len(result.items)} elem a kért {count} helyett"
    return True, f"{len(result.items)} érvényes elem"


def run_checks(question: dict, text: str, source_count: int) -> dict[str, dict]:
    """{check name: {"ok": bool, "note": str}} for the checks this question asks for."""
    expect = question.get("expect", {})
    out: dict[str, dict] = {}

    def add(name: str, ok: bool, note: str = "") -> None:
        out[name] = {"ok": ok, "note": note}

    answer = strip_thinking(text)
    if not answer:
        add("nem üres", False, "üres válasz")
        return out
    if expect.get("json"):
        add("érvényes JSON", *check_json(answer, expect["json"], source_count, question.get("count")))
        return out
    add("magyarul", looks_hungarian(answer), "szókincs- és ékezetarány alapján")
    if expect.get("cite"):
        used = citations.cited_numbers(answer, source_count)
        add("forráshivatkozás", bool(used), f"hivatkozott: {used}" if used else "nincs [n] hivatkozás")
    if expect.get("refuse"):
        lowered = answer.lower()
        refused = any(p in lowered for p in REFUSAL_PHRASES)
        invented = [w for w in expect.get("must_not_contain", []) if w.lower() in lowered]
        known = set(re.findall(r"\d{3,}", " ".join([question["question"]] + [s.get("text", "") for s in question.get("sources", [])])))
        invented += [n for n in re.findall(r"\d{3,}", answer) if n not in known]  # a number from nowhere
        add("elutasít, ha nincs a forrásban", refused and not invented, f"kitalált: {invented}" if invented else "")
    elif expect.get("must_contain_any"):
        found = [w for w in expect["must_contain_any"] if w.lower() in answer.lower()]
        add("tartalmazza a várt tényt", bool(found), f"megtalált: {found}" if found else "hiányzik")
    if expect.get("uncertain"):
        add("jelzi a bizonytalan forrást", "bizonytalan" in answer.lower(), "")
    return out


# --- running ---------------------------------------------------------------------------------------------


def parse_generation(lines: Iterator[str], started: float) -> dict:
    """Consume a streamed /api/generate response; time stamps are taken as lines arrive."""
    text, first, final, error = [], None, {}, None
    for line in lines:
        if not line.strip():
            continue
        try:
            chunk = json.loads(line)
        except json.JSONDecodeError:
            continue
        if chunk.get("error"):
            error = str(chunk["error"])
            break
        piece = chunk.get("response", "")
        if piece and first is None:
            first = time.monotonic() - started
        text.append(piece)
        if chunk.get("done"):
            final = chunk
    total = time.monotonic() - started
    eval_count, eval_ns = final.get("eval_count"), final.get("eval_duration")
    return {
        "text": "".join(text),
        "error": error if error else (None if final else "the stream ended without a final message"),
        "ttft_s": round(first, 3) if first is not None else None,
        "total_s": round(total, 3),
        "load_s": round(final.get("load_duration", 0) / 1e9, 3) if final else None,
        "prompt_tokens": final.get("prompt_eval_count"),
        "output_tokens": eval_count,
        "tokens_per_s": round(eval_count / (eval_ns / 1e9), 2) if eval_count and eval_ns else None,
    }


def find_loaded(transport: Callable, tag: str) -> dict | None:
    models = call_json(transport, "GET", "/api/ps").get("models") or []
    base = tag.split(":")[0]
    return next((m for m in models if m.get("name") == tag or m.get("model") == tag), None) or next(
        (m for m in models if m.get("name", "").split(":")[0] == base), None
    )


def installed_tags(transport: Callable) -> tuple[set[str], str | None]:
    reply = call_json(transport, "GET", "/api/tags")
    if reply.get("error"):
        return set(), reply["error"]
    names = {m.get("name") for m in reply.get("models", [])}
    return names | {n.removesuffix(":latest") for n in names if n}, None


def unload(transport: Callable, tag: str) -> None:
    call_json(transport, "POST", "/api/generate", {"model": tag, "keep_alive": 0, "stream": False})


def evaluate_model(
    candidate: dict, questions: list[dict], transport: Callable, prompt_dir: Path, options: dict, teacher_prompt: Path | None,
    gpu_reader: Callable[[], dict[int, dict] | None], log: Callable[[str], None] = print,
) -> dict:  # fmt: skip
    tag = candidate["tag"]
    baseline = gpu_reader()
    result: dict = {"tag": tag, "answers": [], "gpu_baseline_mib": {str(i): g["used_mib"] for i, g in (baseline or {}).items()}}
    placement: dict = {"placement": "unknown", "gpu_fraction": None, "cpu_fallback": None}
    peaks: dict[int, int] = {}
    for question in questions:
        prompt, json_mode, shown = build_prompt(question, prompt_dir, teacher_prompt)
        body = {"model": tag, "prompt": prompt, "stream": True, "options": {**options, **candidate.get("options", {})}}
        if json_mode:
            body["format"] = "json"
        if candidate.get("think") is False:
            body["think"] = False  # newer Ollama only; older versions ignore it, see strip_thinking
        log(f"  {question['id']} ...")
        started = time.monotonic()
        with GpuSampler(gpu_reader) as sampler:
            run = parse_generation(transport("POST", "/api/generate", body), started)
        for index, mib in sampler.peak.items():
            peaks[index] = max(peaks.get(index, 0), mib)
        if run["error"]:
            log(f"    error: {run['error']}")
        else:
            if placement["placement"] == "unknown":  # the model is loaded now; ask where it went
                placement = classify_placement(find_loaded(transport, tag), len(vram_delta(baseline, peaks)))
            checks = run_checks(question, run["text"], shown)
            run["checks"] = checks
        run.update(question_id=question["id"], answer=strip_thinking(run["text"]))
        run.pop("text")
        result["answers"].append(run)
    delta = vram_delta(baseline, peaks)
    if placement["placement"] in ("single-gpu", "multi-gpu"):  # refine with what nvidia-smi saw
        placement = classify_placement({"size": 1, "size_vram": 1}, len(delta))
    result.update(placement)
    result["vram_added_mib"] = delta
    result["gpus_total_mib"] = {str(i): g["total_mib"] for i, g in (baseline or {}).items()}
    return result


def summarise(model: dict) -> dict:
    good = [a for a in model["answers"] if not a.get("error")]

    def mean(key: str, items=good):
        values = [a[key] for a in items if a.get(key) is not None]
        return round(statistics.mean(values), 2) if values else None

    checks = [c["ok"] for a in good for c in a.get("checks", {}).values()]
    first = good[0] if good else {}
    return {
        "first_ttft_s": first.get("ttft_s"),
        "first_load_s": first.get("load_s"),
        "mean_ttft_s": mean("ttft_s", good[1:] or good),  # warm: the first request includes the model load
        "mean_tokens_per_s": mean("tokens_per_s"),
        "total_time_s": round(sum(a["total_s"] for a in model["answers"]), 1),
        "errors": len(model["answers"]) - len(good),
        "checks_passed": sum(checks),
        "checks_total": len(checks),
    }


# --- questions & candidates ---------------------------------------------------------------------------------


def load_questions(paths: list[Path]) -> list[dict]:
    questions: list[dict] = []
    for path in paths:
        data = json.loads(path.read_text(encoding="utf-8"))
        items = data["questions"] if isinstance(data, dict) else data
        for item in items:
            for field in ("id", "question"):
                if not item.get(field):
                    raise SystemExit(f"{path}: every question needs an '{field}'")
            if item.get("task", "answer") not in ("answer", "summary", "quiz", "flashcards"):
                raise SystemExit(f"{path}: unknown task in {item['id']}")
            questions.append(item)
    ids = [q["id"] for q in questions]
    if len(set(ids)) != len(ids):
        raise SystemExit("question ids must be unique across the files")
    return questions


def load_candidates(path: Path, only: list[str] | None) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    candidates = data["candidates"] if isinstance(data, dict) else data
    if only:
        candidates = [c for c in candidates if any(o in c["tag"] for o in only)]
    if not candidates:
        raise SystemExit("no candidates selected")
    return candidates


# --- report ------------------------------------------------------------------------------------------------------

RATING_LINE = "**Értékelés (1-5):**"
RATING_RE = re.compile(r"^\*\*Értékelés \(1-5\):\*\*[ \t]*([1-5])?[ \t]*$")


def label_models(tags: list[str], blind: bool, seed: int) -> dict[str, str]:
    if not blind:
        return {t: t for t in tags}
    order = list(tags)
    random.Random(seed).shuffle(order)
    return {t: f"Modell {chr(ord('A') + i)}" for i, t in enumerate(order)}


def _fmt(value, unit: str = "") -> str:
    return "–" if value is None else f"{value}{unit}"


def describe_placement(model: dict) -> str:
    kind, fraction = model["placement"], model["gpu_fraction"]
    if kind == "gpu+cpu":
        return f"részben CPU ({fraction:.0%} GPU)"
    return {
        "cpu": "CPU-ra esett vissza", "multi-gpu": "több GPU között osztva", "single-gpu": "egy GPU",
        "unknown": "ismeretlen",
    }[kind]  # fmt: skip


def render_report(run: dict) -> str:
    models, labels, questions = run["models"], run["labels"], run["questions"]
    lines = [f"# Modellértékelés – {run['started']}", ""]
    lines += [
        "Töltsd ki az **Értékelés (1-5)** sorokat (1 = használhatatlan, 5 = kiváló magyarsággal és tartalmilag is),",
        f"majd: `python3 scripts/eval_models.py totals {run['report_name']}`.",
        "A gépi ellenőrzések csak a formát és a forráskezelést nézik, a nyelvi minőséget nem.",
    ]
    if run["blind"]:
        lines.append("A modellek neve el van rejtve; a megfejtés a `results.json`-ban van, a `totals` ezt használja.")
    lines += ["", "## Mérések", ""]
    if not run["blind"]:
        lines += [
            "| Modell | Első tokenig (hideg) | Betöltés | Átlag tok/s | Összidő | GPU-használat (MiB/GPU) | Elhelyezés | Ellenőrzések |",
            "|---|---|---|---|---|---|---|---|",
        ]
        for m in models:
            s = m["summary"]
            vram = ", ".join(f"GPU{i}: {mib}" for i, mib in m["vram_added_mib"].items()) or "nincs adat"
            where = describe_placement(m)
            lines.append(
                f"| {m['tag']} | {_fmt(s['first_ttft_s'], ' s')} | {_fmt(s['first_load_s'], ' s')} | "
                f"{_fmt(s['mean_tokens_per_s'])} | {s['total_time_s']} s | {vram} | {where} | "
                f"{s['checks_passed']}/{s['checks_total']} |"
            )
        lines += ["", "Az első tokenig mért idő a hideg betöltést is tartalmazza (az első kérdésnél).", ""]
    else:
        lines += ["A mérések a `results.json`-ban vannak (a név elrejtése miatt itt nem szerepelnek).", ""]
    lines += ["## Kérdések és válaszok", ""]
    by_tag = {m["tag"]: {a["question_id"]: a for a in m["answers"]} for m in models}
    ordered = sorted(models, key=lambda m: labels[m["tag"]])
    for q in questions:
        lines += [f"### {q['id']} – {q.get('title', q['question'])[:90]}", "", f"> {q['question']}", ""]
        for m in ordered:
            a = by_tag[m["tag"]][q["id"]]
            lines += [f"#### {labels[m['tag']]}", ""]
            if a.get("error"):
                lines += [f"*Hiba: {a['error']}*", ""]
            else:
                quoted = a["answer"].replace("\n", "\n> ") or "(üres)"
                lines += [f"> {quoted}", ""]
                checks = a.get("checks", {})
                if checks:
                    lines.append(" · ".join(f"{'✔' if c['ok'] else '✘'} {n}" + (f" ({c['note']})" if c["note"] and not c["ok"] else "") for n, c in checks.items()))
                    lines.append("")
            lines += [RATING_LINE, ""]
    return "\n".join(lines) + "\n"


def total_ratings(report: str, results: dict) -> dict[str, dict]:
    """{tag: {"sum", "count", "mean"}} from the filled-in report; blank rating lines are skipped."""
    by_label = {label: tag for tag, label in results["labels"].items()}
    totals: dict[str, dict] = {tag: {"sum": 0, "count": 0, "mean": None} for tag in results["labels"]}
    current: str | None = None
    for line in report.splitlines():
        if line.startswith("#### "):
            current = by_label.get(line[5:].strip())
        elif line.startswith("### "):
            current = None
        else:
            match = RATING_RE.match(line.strip())
            if match and match.group(1) and current:
                totals[current]["sum"] += int(match.group(1))
                totals[current]["count"] += 1
    for t in totals.values():
        t["mean"] = round(t["sum"] / t["count"], 2) if t["count"] else None
    return totals


# --- guards & commands ------------------------------------------------------------------------------------------


def ensure_ignored(path: Path) -> None:
    """Refuse to write results inside the repo unless git ignores the place (they hold private answers)."""
    resolved = path.resolve()
    try:
        resolved.relative_to(ROOT)
    except ValueError:
        return  # outside the repo
    probe = resolved / "x"
    check = subprocess.run(["git", "-C", str(ROOT), "check-ignore", "-q", str(probe)], capture_output=True)  # noqa: S603, S607
    if check.returncode != 0:
        raise SystemExit(f"{path} is inside the repository and not gitignored; pick eval-results/ or a path outside it")


def cmd_run(args: argparse.Namespace) -> int:
    private = [Path(p) for p in args.private]
    for p in private:
        if not p.is_file():
            raise SystemExit(f"private question file not found: {p}")
        ensure_ignored(p.parent)
    questions = load_questions([Path(args.questions)] + private)
    if args.only_questions:
        questions = [q for q in questions if any(o in q["id"] for o in args.only_questions)]
    candidates = load_candidates(Path(args.candidates), args.only)
    out_root = Path(args.out)
    ensure_ignored(out_root)
    transport = url_transport(args.url) if args.url else compose_transport()
    installed, problem = installed_tags(transport)
    if problem:
        raise SystemExit(
            f"cannot reach Ollama: {problem}\n(by default this goes through `docker compose exec api`; "
            "start the stack with make dev-up, or pass --url)"
        )
    missing = [c["tag"] for c in candidates if c["tag"] not in installed]
    if missing:
        print(f"Not installed, skipped: {', '.join(missing)}")
        print(f"  fetch with: scripts/model-update.sh {' '.join(missing)}")
        candidates = [c for c in candidates if c["tag"] in installed]
        if not candidates:
            return 1
    gpu_reader = read_gpus if not args.no_gpu else (lambda: None)
    if gpu_reader() is None:
        print("nvidia-smi not available on this host: per-GPU memory will not be reported.")
    options = {**DEFAULT_OPTIONS, "num_ctx": args.num_ctx}
    started = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    models, previous = [], None
    for candidate in candidates:
        print(f"{candidate['tag']}")
        if previous:
            unload(transport, previous)  # each model starts cold on an idle GPU
        previous = candidate["tag"]
        model = evaluate_model(
            candidate, questions, transport, DEFAULT_PROMPT_DIR, options,
            Path(args.teacher_prompt) if args.teacher_prompt else None, gpu_reader,
        )  # fmt: skip
        model["summary"] = summarise(model)
        model["candidate"] = candidate
        models.append(model)
    if previous:
        unload(transport, previous)
    run_dir = out_root / datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir.mkdir(parents=True)
    labels = label_models([m["tag"] for m in models], not args.no_blind, args.seed)
    run = {
        "started": started, "options": options, "blind": not args.no_blind, "seed": args.seed, "labels": labels,
        "report_name": str(run_dir / "report.md"), "questions": questions, "models": models,
        "private_files": [str(p) for p in private],
    }  # fmt: skip
    (run_dir / "results.json").write_text(json.dumps(run, ensure_ascii=False, indent=2), encoding="utf-8")
    (run_dir / "report.md").write_text(render_report(run), encoding="utf-8")
    print(f"\nResults: {run_dir / 'results.json'}\nReport:  {run_dir / 'report.md'}")
    return 0


def cmd_totals(args: argparse.Namespace) -> int:
    report = Path(args.report)
    results = json.loads((report.parent / "results.json").read_text(encoding="utf-8"))
    totals = total_ratings(report.read_text(encoding="utf-8"), results)
    summaries = {m["tag"]: m["summary"] for m in results["models"]}
    print(f"{'model':45} {'rated':>5} {'sum':>5} {'mean':>5}  checks  tok/s")
    for tag, t in sorted(totals.items(), key=lambda kv: -(kv[1]["mean"] or 0)):
        s = summaries[tag]
        mean = f"{t['mean']:.2f}" if t["mean"] is not None else "-"
        print(
            f"{tag:45} {t['count']:>5} {t['sum']:>5} {mean:>5}  {s['checks_passed']}/{s['checks_total']}   {_fmt(s['mean_tokens_per_s'])}"
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="run the candidates against the questions")
    run.add_argument("--candidates", required=True, help="candidate config (JSON), e.g. config/models/eval_candidates.example.json")
    run.add_argument("--questions", default=str(DEFAULT_QUESTIONS), help="built-in question set")
    run.add_argument("--private", action="append", default=[], metavar="FILE", help="extra question/passage file kept outside git (repeatable)")
    run.add_argument("--out", default=str(DEFAULT_OUT), help="results directory (must be gitignored if inside the repo)")
    run.add_argument("--url", help="talk to this Ollama directly instead of through the api container")
    run.add_argument("--only", action="append", help="only candidates whose tag contains this (repeatable)")
    run.add_argument("--only-questions", action="append", help="only questions whose id contains this (repeatable)")
    run.add_argument("--teacher-prompt", help="teacher prompt file (default config/prompts/teacher_hu.md)")
    run.add_argument("--num-ctx", type=int, default=DEFAULT_OPTIONS["num_ctx"])
    run.add_argument("--no-blind", action="store_true", help="show model names in the report")
    run.add_argument("--no-gpu", action="store_true", help="do not call nvidia-smi")
    run.add_argument("--seed", type=int, default=0, help="seed of the blind labelling")
    run.set_defaults(func=cmd_run)
    totals = sub.add_parser("totals", help="total the ratings filled into a report")
    totals.add_argument("report", help="report.md next to its results.json")
    totals.set_defaults(func=cmd_totals)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
