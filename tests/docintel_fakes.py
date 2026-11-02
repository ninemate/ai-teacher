"""A fake language model and small documents for the Document Intelligence tests.

The fake reads the numbered passages out of the prompt the way a real model would and answers in the JSON the
prompts ask for, so tests assert structure, citations and storage, never model wording. It also cites a number that
does not exist (INVENTED) in every answer, to prove such numbers are dropped.
"""

import json
import re
from pathlib import Path

from teacher_common import citations, docintel

ROOT = Path(__file__).resolve().parents[1]
INVENTED = 999

MARKERS = {
    "summary_map": "a dokumentum alábbi részének összefoglalása",
    "summary_reduce": "részösszefoglalóinak összevonása",
    "extract": "strukturált adatok kigyűjtése",
    "tasks_map": "a teendők, kötelezettségek, határidők és követelmények kigyűjtése",
    "tasks_propose": "megoldási javaslatok készítése",
    "compare": "Jelölt fajták",
    "translate_hu_en": "magyar szöveget angolra",
    "translate_en_hu": "angol szöveget magyarra",
}
PREFIXES = {
    "Követelmény:": "requirement",
    "Határidő:": "deadline",
    "Kötelezettség:": "obligation",
    "Fogalom:": "concept",
    "Teendő:": "task",
}


def passages(prompt: str) -> list[tuple[int, str]]:
    """(number, text) of every numbered line of the prompt's context."""
    return [(int(n), line.rsplit(" | ", 1)[-1]) for n, line in re.findall(r"^\[(\d+)\] (.*)$", prompt, re.M)]


def kind_of(prompt: str) -> str:
    return next(kind for kind, marker in MARKERS.items() if marker in prompt)


class FakeModel:
    """Callable(prompt, json_mode) -> text. ``script`` entries (strings or exceptions) are used first, in order."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []  # (kind, prompt)
        self.script: list = []
        self.fail_on: int | None = None  # raise on the n-th call (1-based), once

    def __call__(self, prompt: str, json_mode: bool) -> str:
        kind = kind_of(prompt)
        self.calls.append((kind, prompt))
        if self.fail_on == len(self.calls):
            self.fail_on = None
            raise RuntimeError("simulated crash")
        if self.script:
            item = self.script.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        return getattr(self, f"answer_{kind}")(prompt)

    def count(self, kind: str) -> int:
        return sum(1 for k, _ in self.calls if k == kind)

    def answer_summary_map(self, prompt: str) -> str:
        found = passages(prompt)
        return json.dumps({"points": [{"text": f"Pont: {found[0][1][:40]}", "sources": [1, INVENTED]}]})

    def answer_summary_reduce(self, prompt: str) -> str:
        found = passages(prompt)
        points = [{"text": f"Összevont: {found[i][1][:30]}", "sources": [found[i][0], found[i + 1][0]]}
                  for i in range(0, len(found) - 1, 2)] or [{"text": f"Összevont: {found[0][1][:30]}", "sources": [1]}]
        return json.dumps({"points": points})

    def answer_extract(self, prompt: str, allowed: tuple[str, ...] = ()) -> str:
        items = []
        for number, text in passages(prompt):
            for sentence in re.split(r"(?<=\.)\s+", text):
                for prefix, kind in PREFIXES.items():
                    if sentence.startswith(prefix) and kind != "task":
                        items.append({"type": kind, "text": sentence[len(prefix):].strip(), "detail": "",
                                      "sources": [number, INVENTED]})
                if sentence.startswith("Táblázat:"):
                    items.append({"type": "table", "text": "Díjak", "detail": "", "sources": [number],
                                  "table": {"columns": ["Tétel", "Összeg"], "rows": [["Tagdíj", "1000"]]}})
        return json.dumps({"items": items})

    def answer_tasks_map(self, prompt: str) -> str:
        items = [{"type": "task", "text": sentence[len("Teendő:"):].strip(), "detail": "", "sources": [number]}
                 for number, text in passages(prompt) for sentence in re.split(r"(?<=\.)\s+", text)
                 if sentence.startswith("Teendő:")]
        return json.dumps({"items": items})

    def answer_tasks_propose(self, prompt: str) -> str:
        found = passages(prompt)
        proposals = [{"items": [number], "proposal": f"Javaslat a(z) {number}. elemhez"} for number, _ in found]
        proposals.append({"items": [INVENTED], "proposal": "Általános javaslat forrás nélkül"})
        return json.dumps({"proposals": proposals})

    def answer_compare(self, prompt: str) -> str:
        verdicts = []
        for number, text in passages(prompt):
            verdict = "equivalent" if "EKVIVALENS" in text else "different"
            verdicts.append({"candidate": number, "verdict": verdict, "description": f"Eltérés: {number}"})
        verdicts.append({"candidate": INVENTED, "verdict": "different", "description": "kitalált"})
        return json.dumps({"verdicts": verdicts})

    def answer_translate_hu_en(self, prompt: str) -> str:
        return "[EN] " + prompt.split("Szöveg:\n", 1)[1].strip()

    def answer_translate_en_hu(self, prompt: str) -> str:
        return "[HU] " + prompt.split("Szöveg:\n", 1)[1].strip()


def make_doc(name: str, pages: list[str], *, chapters: bool = False, title: str | None = None) -> dict:
    """A document as the worker loads it: one chunk per page (or chapter), in reading order."""
    chunks = []
    for index, text in enumerate(pages, start=1):
        chunks.append({
            "chunk_id": f"{name}:{index}", "file_id": name, "file_name": name, "source_path": f"/library/{name}",
            "title": title or name, "language": "hu", "position": index,
            "locator": f"fejezet {index}" if chapters else f"{index}. oldal",
            "page": None if chapters else index, "chapter": str(index) if chapters else None,
            "ocr": False, "ocr_confidence": None, "preview": text[:240], "text": text, "score": None,
        })
    return {"source_path": f"/library/{name}", "file_name": name, "title": title or name, "chunks": chunks}


def make_runner(model, *, parts=None, window_chars=700, translate_chars=700, **extra) -> docintel.Runner:
    return docintel.Runner(
        llm=model,
        prompts=lambda name: docintel.load_prompt(ROOT / "config" / "prompts", name),
        limits=citations.Limits(weak_score=0.8, ocr_min_confidence=70),
        window_chars=window_chars,
        translate_chars=translate_chars,
        parts={} if parts is None else parts,
        save_part=(lambda key, value: parts.__setitem__(key, value)) if parts is not None else (lambda k, v: None),
        **extra,
    )
