"""Whole-document jobs: summary, extraction, comparison, tasks and translation, as map-reduce over the chunks.

Pure logic (no database, no HTTP): the model is a callable, the checkpoint store a dict, so the whole of it runs
in tests with a fake model. ``docintel_worker`` runs it in the background and stores progress and results.

How a job reads a document
    The chunks come in reading order (``doc_text``) and are cut into windows that fit the configured context.
    Each window is one model call. Its passages are numbered 1..k with ``citations.build_citations`` /
    ``build_context``, the model answers in JSON and cites those numbers, and every number outside 1..k is dropped
    (``valid_sources``). An item left without a valid source is rejected, never shown as sourced. Reduce steps
    number the earlier items the same way, so the chain from a final statement down to the page stays checkable.

Items carry the ids of the chunks they stand on (``src``) while the job runs; ``finalize`` numbers the cited chunks
1..N in reading order, which gives the result its citation list, and ``citations.assess`` rates the whole result
the way it rates a chat answer.
"""

import csv
import io
import json
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from teacher_common import citations, doc_text, study

KINDS = ("summary", "extract", "compare", "tasks", "translate")
EXTRACT_TYPES = ("concept", "requirement", "deadline", "obligation", "table")
TASK_TYPES = ("task", "obligation", "deadline", "requirement")
COMPARE_TYPES = ("requirement", "obligation", "deadline")
TYPE_LABELS = {
    "concept": "fogalom",
    "requirement": "követelmény",
    "deadline": "határidő",
    "obligation": "kötelezettség",
    "table": "táblázat",
    "task": "teendő",
}
TRANSLATE_DIRECTIONS = {"hu-en": "docintel_translate_hu_en.md", "en-hu": "docintel_translate_en_hu.md"}
PROMPTS = {
    "summary_map": "docintel_summary_map_hu.md",
    "summary_reduce": "docintel_summary_reduce_hu.md",
    "extract": "docintel_extract_hu.md",
    "tasks_map": "docintel_tasks_map_hu.md",
    "tasks_propose": "docintel_tasks_propose_hu.md",
    "compare": "docintel_compare_hu.md",
    **{f"translate_{key}": name for key, name in TRANSLATE_DIRECTIONS.items()},
}

SUMMARY_POINTS = 8
# A reduce pass runs while more than this many points (or characters) are left.
REDUCE_TARGET = 10
MAX_REDUCE_LEVELS = 6
PAGES_PER_SECTION = 10
CHUNKS_PER_SECTION = 25
# Lexical heuristics for comparing statements; word stems of 5 letters stand in for Hungarian morphology.
ROW_SAME = 0.8
ROW_RELATED = 0.3
ABSENT_BELOW = 0.5
CLOSEST_FROM = 0.3
JUDGE_BATCH = 12
TRANSLATION_RATIO = (0.2, 6.0)


class JobCancelled(Exception):
    """The person stopped the job; the worker records it as cancelled."""


class JobFailed(Exception):
    """A clear, Hungarian reason the job cannot produce a result."""


class OutputError(Exception):
    """The model did not produce usable output within the allowed attempts."""


# --- running context -----------------------------------------------------------------------------------------------


@dataclass
class Runner:
    """Everything a job needs from the outside, so tests can replace the model and the store."""

    llm: Callable[[str, bool], str]
    prompts: Callable[[str], str]
    limits: citations.Limits
    window_chars: int
    translate_chars: int
    max_attempts: int = 2
    parts: dict[str, Any] = field(default_factory=dict)
    save_part: Callable[[str, Any], None] = lambda key, value: None
    on_progress: Callable[[int, int, str], None] = lambda done, total, phase: None
    should_cancel: Callable[[], bool] = lambda: False
    done: int = 0
    total: int = 0
    phase: str = ""
    # What went wrong without stopping the job: windows given up on, reduce steps that fell back.
    failed_places: list[str] = field(default_factory=list)
    windows_total: int = 0
    rejected: int = 0
    warnings: list[str] = field(default_factory=list)

    def plan(self, steps: int, phase: str) -> None:
        self.total += steps
        self.phase = phase
        self.on_progress(self.done, self.total, phase)

    def step(self, key: str, compute: Callable[[], Any], phase: str | None = None) -> Any:
        """One resumable unit of work: its stored output when a previous run finished it, else compute and keep."""
        if self.should_cancel():
            raise JobCancelled
        if phase:
            self.phase = phase
        if key in self.parts:
            value = self.parts[key]
        else:
            value = compute()
            # A step that gave up (a "failed" marker) is retried by a resume, not remembered.
            if not (isinstance(value, dict) and value.get("failed")):
                self.parts[key] = value
                self.save_part(key, value)
        self.done += 1
        self.on_progress(self.done, self.total, self.phase)
        return value

    def ask(self, prompt: str, parse: Callable[[str], Any], json_mode: bool = True) -> Any:
        """Ask the model, parse the answer; on unusable output ask again once with the problem named, then give up."""
        problem = "ismeretlen hiba"
        for attempt in range(self.max_attempts):
            request = prompt
            if attempt:
                request += (
                    f"\n\nAz előző válaszod nem volt használható ({problem}). "
                    + ("Add vissza újra, KIZÁRÓLAG a kért JSON szerkezetben." if json_mode else "Add vissza újra.")
                )
            try:
                return parse(self.llm(request, json_mode))
            except ValueError as exc:
                problem = str(exc) or "üres válasz"
        raise OutputError(problem)


def render(template: str, **values: str) -> str:
    """Fill the placeholders; the document text goes in last so a ``{...}`` inside it is never expanded."""
    last = {key: values.pop(key) for key in ("context", "text") if key in values}
    for key, value in values.items():
        template = template.replace("{" + key + "}", value)
    for key, value in last.items():
        template = template.replace("{" + key + "}", value)
    return template


# --- grounding -----------------------------------------------------------------------------------------------------


def valid_sources(raw: Any, count: int) -> list[int]:
    """The source numbers 1..count the model cited; anything else it wrote is an invented number and dropped."""
    if isinstance(raw, int):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    numbers = [str(n) if isinstance(n, int) and not isinstance(n, bool) else str(n).strip() for n in raw]
    # Same rule as the chat: reuse the marker reader, so both agree on what counts as a citation.
    return citations.cited_numbers("[" + ",".join(n for n in numbers if n.isdigit()) + "]", count)


def place_label(chunk: dict) -> str:
    page, chapter = chunk.get("page"), chunk.get("chapter")
    if page is not None:
        return f"{page}. oldal"
    if chapter:
        return f"{chapter}. fejezet" if str(chapter).isdigit() else f"{chapter} fejezet"
    return chunk.get("locator") or "ismeretlen hely"


def _window_context(window: Sequence[dict], limits: citations.Limits) -> tuple[str, list[dict]]:
    # No truncation here: `make_windows` already sized the window to the configured context.
    built = citations.build_citations(window, limits)
    context, used = citations.build_context(built, window, 10**9)
    return context, used


def _failure_place(window: Sequence[dict]) -> str:
    first, last = place_label(window[0]), place_label(window[-1])
    name = window[0].get("file_name") or ""
    return f"{name}, {first}" if first == last else f"{name}, {first} – {last}"


# --- parsed model output -------------------------------------------------------------------------------------------


class _Sourced(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    sources: list[Any] = Field(default_factory=list)

    @field_validator("sources", mode="before")
    @classmethod
    def _listify(cls, value: Any) -> Any:
        return [value] if isinstance(value, int) else value


class PointModel(_Sourced):
    text: str = Field(min_length=3)


class ItemModel(_Sourced):
    type: str
    text: str = Field(min_length=2)
    detail: str | None = None
    table: dict | None = None

    @field_validator("type")
    @classmethod
    def _norm(cls, value: str) -> str:
        return value.strip().lower()


class ProposalModel(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    items: list[Any] = Field(default_factory=list)
    proposal: str = Field(min_length=3)

    @field_validator("items", mode="before")
    @classmethod
    def _listify(cls, value: Any) -> Any:
        return [value] if isinstance(value, int) else value


class VerdictModel(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    candidate: int
    verdict: str
    description: str = ""


def _json_list(text: str, key: str) -> list:
    data = study.extract_json(text)
    raw = data.get(key) if isinstance(data, dict) else data
    if not isinstance(raw, list):
        raise ValueError(f'hiányzik a "{key}" lista')
    return raw


def _table_ok(table: Any) -> dict | None:
    if not isinstance(table, dict):
        return None
    columns, rows = table.get("columns"), table.get("rows")
    if not isinstance(columns, list) or not columns or not isinstance(rows, list) or not rows:
        return None
    # Every row as wide as the header: a ragged table would render misaligned and lose cells in the CSV.
    if not all(isinstance(row, list) and len(row) == len(columns) for row in rows):
        return None
    return {"columns": [str(c).strip() for c in columns], "rows": [[str(cell).strip() for cell in row] for row in rows]}


def _parse_sourced(
    text: str, key: str, model: type[BaseModel], used: Sequence[dict], *, allowed: Sequence[str] | None = None
) -> tuple[list[dict], int]:
    """Validated items of one window with ``src`` = ids of the chunks they cite; (items, number rejected).

    A non-empty list in which nothing survives is an error (the model is asked again); an empty list is an answer.
    """
    raw_items = _json_list(text, key)
    items: list[dict] = []
    rejected = 0
    for raw in raw_items:
        try:
            parsed = model.model_validate(raw)
            numbers = valid_sources(parsed.sources, len(used))  # type: ignore[attr-defined]
            if not numbers:
                raise ValueError("nincs érvényes forrás")
            item = parsed.model_dump(exclude={"sources"})
            if allowed is not None and item["type"] not in allowed:
                raise ValueError("nem kért típus")
            if item.get("type") == "table":
                item["table"] = _table_ok(item.get("table"))
                if item["table"] is None:
                    raise ValueError("hibás táblázat")
            else:
                item.pop("table", None)
            item["src"] = [used[n - 1]["chunk_id"] for n in numbers]
            items.append(item)
        except (ValidationError, ValueError, TypeError):
            rejected += 1
    if raw_items and not items:
        raise ValueError("a lista egyetlen eleme sem volt érvényes forrással ellátva")
    return items, rejected


# --- map over windows ----------------------------------------------------------------------------------------------


def map_windows(
    runner: Runner,
    prefix: str,
    windows: Sequence[Sequence[dict]],
    template: str,
    *,
    key: str,
    model: type[BaseModel],
    allowed: Sequence[str] | None = None,
    values: dict[str, str] | None = None,
) -> list[dict]:
    """Run the template over every window; the items of all windows, in reading order.

    A window the model cannot answer usably after the allowed attempts is given up on and reported in the result
    (``failed_places``) rather than ending a job of hundreds of windows; ``finalize`` fails the job when too much is lost.
    """
    items: list[dict] = []
    runner.windows_total += len(windows)
    for index, window in enumerate(windows):

        def compute(window=window) -> dict:
            context, used = _window_context(window, runner.limits)
            prompt = render(template, context=context, **(values or {}))
            try:
                found, rejected = runner.ask(prompt, lambda text: _parse_sourced(text, key, model, used, allowed=allowed))
            except OutputError as exc:
                return {"failed": str(exc), "place": _failure_place(window)}
            return {"items": found, "rejected": rejected}

        part = runner.step(f"{prefix}:{index}", compute)
        if part.get("failed"):
            runner.failed_places.append(part["place"])
            continue
        runner.rejected += part["rejected"]
        items.extend(part["items"])
    return items


def _dedupe(items: list[dict]) -> list[dict]:
    """Merge items saying the same thing (the chunk overlap and repeated headers produce them); keep all sources."""
    merged: dict[tuple, dict] = {}
    for item in items:
        key = (item.get("type"), re.sub(r"\W+", " ", item["text"].casefold()).strip())
        if item.get("type") == "table":
            # Two tables under the same caption are two tables unless their cells are the same too.
            table = item.get("table") or {}
            key += (json.dumps([table.get("columns"), table.get("rows")], ensure_ascii=False).casefold(),)
        if key in merged:
            merged[key]["src"] = list(dict.fromkeys(merged[key]["src"] + item["src"]))
        else:
            merged[key] = {**item, "src": list(item["src"])}
    return list(merged.values())


# --- summary -------------------------------------------------------------------------------------------------------


def split_sections(chunks: Sequence[dict], granularity: str, pages_per_section: int = PAGES_PER_SECTION) -> list[dict]:
    """The parts of a document summarised on their own: the chapters, else page ranges, else runs of chunks."""
    if granularity != "chapter" or not chunks:
        return [{"label": "Teljes dokumentum", "chunks": list(chunks)}] if chunks else []
    sections: list[dict] = []
    if any(c.get("chapter") for c in chunks):
        current_key = object()
        for chunk in chunks:
            key = chunk.get("chapter") or (sections and sections[-1]["key"])
            if not sections or key != current_key:
                sections.append({"key": key, "label": place_label(chunk), "chunks": []})
                current_key = key
            sections[-1]["chunks"].append(chunk)
    elif any(c.get("page") is not None for c in chunks):
        for chunk in chunks:
            page = chunk.get("page") or 1
            first = ((page - 1) // pages_per_section) * pages_per_section + 1
            if not sections or sections[-1]["first"] != first:
                sections.append({"first": first, "label": f"{first}–{first + pages_per_section - 1}. oldal", "chunks": []})
            sections[-1]["chunks"].append(chunk)
        for section in sections:
            pages = [c["page"] for c in section["chunks"] if c.get("page") is not None]
            section["label"] = f"{min(pages)}. oldal" if min(pages) == max(pages) else f"{min(pages)}–{max(pages)}. oldal"
    else:
        for start in range(0, len(chunks), CHUNKS_PER_SECTION):
            sections.append({"label": f"{start // CHUNKS_PER_SECTION + 1}. rész", "chunks": chunks[start : start + CHUNKS_PER_SECTION]})
    return sections


def _numbered(items: Sequence[dict], line: Callable[[dict], str]) -> str:
    return "\n".join(f"[{number}] {line(item)}" for number, item in enumerate(items, start=1))


def _batches(items: Sequence[dict], line: Callable[[dict], str], budget: int, per_batch: int = 40) -> list[list[dict]]:
    batches: list[list[dict]] = []
    current: list[dict] = []
    size = 0
    for item in items:
        cost = len(line(item)) + 8
        if current and (size + cost > budget or len(current) >= per_batch):
            batches.append(current)
            current, size = [], 0
        current.append(item)
        size += cost
    if current:
        batches.append(current)
    return batches


def _reduce_to_references(
    runner: Runner,
    prefix: str,
    items: list[dict],
    template: str,
    *,
    key: str,
    line: Callable[[dict], str],
    values: dict[str, str],
    budget: int,
) -> list[dict]:
    """Merge ``items`` into fewer ones, level by level, until they fit one call; ``src`` is inherited from the
    numbered inputs each new point cites, so nothing in the output stands on an invented source."""
    level = 0
    while level < MAX_REDUCE_LEVELS:
        batches = _batches(items, line, budget)
        if len(batches) == 1 and len(items) <= REDUCE_TARGET:
            break
        runner.plan(sum(len(batch) > 1 for batch in batches), "Összevonás")
        reduced: list[dict] = []
        for index, batch in enumerate(batches):
            if len(batch) == 1:
                reduced.extend(batch)  # nothing to merge it with
                continue

            def compute(batch=batch) -> dict:
                prompt = render(template, context=_numbered(batch, line), **values)

                def parse(text: str) -> list[dict]:
                    out: list[dict] = []
                    for raw in _json_list(text, key):
                        try:
                            point = PointModel.model_validate(raw)
                        except ValidationError:
                            continue
                        numbers = valid_sources(point.sources, len(batch))
                        if numbers:
                            src = list(dict.fromkeys(s for n in numbers for s in batch[n - 1]["src"]))
                            out.append({"text": point.text, "src": src})
                    if not out:
                        raise ValueError("a pontok egyike sem hivatkozott érvényes sorszámra")
                    return out

                try:
                    return {"items": runner.ask(prompt, parse)}
                except OutputError:
                    # Better the points unmerged than a failed job or merged ones nobody can check.
                    runner.warnings.append("Az összevonás egy részét nem sikerült elvégezni; ott a részeredmények változatlanok.")
                    return {"items": batch, "fallback": True}

            reduced.extend(runner.step(f"{prefix}:r{level}:{index}", compute)["items"])
        if len(reduced) >= len(items):
            return reduced
        items, level = reduced, level + 1
    return items


def run_summary(runner: Runner, documents: list[dict], options: dict) -> dict:
    granularity = options.get("granularity", "document")
    sections_out: list[dict] = []
    all_chunks: list[dict] = []
    budget = runner.window_chars
    template_map = runner.prompts("summary_map")
    template_reduce = runner.prompts("summary_reduce")
    values = {"max_points": str(SUMMARY_POINTS)}
    prepared = []
    for document in documents:
        all_chunks.extend(document["chunks"])
        for section in split_sections(document["chunks"], granularity):
            prepared.append((document, section, doc_text.make_windows(section["chunks"], runner.window_chars)))
    runner.plan(sum(len(windows) for _, _, windows in prepared), "Szakaszok olvasása")

    def point_line(item: dict) -> str:
        return item["text"]

    for number, (document, section, windows) in enumerate(prepared):
        items = map_windows(
            runner, f"s{number}", windows, template_map, key="points", model=PointModel, values=values
        )
        items = _reduce_to_references(
            runner, f"s{number}", items, template_reduce, key="points", line=point_line, values=values, budget=budget
        )
        label = section["label"] if granularity == "chapter" else document["title"]
        sections_out.append({"label": label, "file_name": document["file_name"], "points": items})
    overall: list[dict] = []
    if granularity == "chapter" or len(documents) > 1:
        merged = [item for section in sections_out for item in section["points"]]
        overall = _reduce_to_references(
            runner, "overall", merged, template_reduce, key="points", line=point_line, values=values, budget=budget
        )
    elif sections_out:
        overall = sections_out[0]["points"]
    show_sections = granularity == "chapter" or len(documents) > 1
    return {"chunks": all_chunks, "overall": overall, "sections": sections_out if show_sections else []}


# --- extraction ----------------------------------------------------------------------------------------------------


def run_extract(runner: Runner, documents: list[dict], options: dict) -> dict:
    types = [t for t in options.get("types") or EXTRACT_TYPES if t in EXTRACT_TYPES] or list(EXTRACT_TYPES)
    template = runner.prompts("extract")
    types_text = "\n".join(f"- {t} ({TYPE_LABELS[t]})" for t in types)
    chunks: list[dict] = []
    windows_by_doc = []
    for document in documents:
        chunks.extend(document["chunks"])
        windows_by_doc.append(doc_text.make_windows(document["chunks"], runner.window_chars))
    runner.plan(sum(len(w) for w in windows_by_doc), "Adatok kigyűjtése")
    items: list[dict] = []
    for number, windows in enumerate(windows_by_doc):
        items.extend(
            map_windows(
                runner, f"e{number}", windows, template, key="items", model=ItemModel, allowed=types,
                values={"types": types_text},
            )
        )
    return {"chunks": chunks, "items": _dedupe(items), "types": types}


# --- tasks ---------------------------------------------------------------------------------------------------------


def run_tasks(runner: Runner, documents: list[dict], options: dict) -> dict:
    chunks: list[dict] = []
    windows_by_doc = []
    for document in documents:
        chunks.extend(document["chunks"])
        windows_by_doc.append(doc_text.make_windows(document["chunks"], runner.window_chars))
    runner.plan(sum(len(w) for w in windows_by_doc), "Teendők kigyűjtése")
    items: list[dict] = []
    for number, windows in enumerate(windows_by_doc):
        items.extend(
            map_windows(runner, f"t{number}", windows, runner.prompts("tasks_map"), key="items", model=ItemModel,
                        allowed=TASK_TYPES)
        )
    items = _dedupe(items)

    def line(item: dict) -> str:
        return f"({TYPE_LABELS.get(item['type'], item['type'])}) {item['text']}" + (f" – {item['detail']}" if item.get("detail") else "")

    batches = _batches(items, line, runner.window_chars)
    runner.plan(len(batches), "Megoldási javaslatok")
    proposals: list[dict] = []
    for index, batch in enumerate(batches):

        def compute(batch=batch) -> dict:
            prompt = render(runner.prompts("tasks_propose"), context=_numbered(batch, line),
                            max_points=str(max(3, len(batch))))

            def parse(text: str) -> list[dict]:
                out: list[dict] = []
                for raw in _json_list(text, "proposals"):
                    try:
                        proposal = ProposalModel.model_validate(raw)
                    except ValidationError:
                        continue
                    numbers = valid_sources(proposal.items, len(batch))
                    # A proposal citing nothing valid is still the model's own idea, shown as such, not as sourced.
                    basis = [batch[n - 1]["_id"] for n in numbers]
                    src = list(dict.fromkeys(s for n in numbers for s in batch[n - 1]["src"]))
                    out.append({"text": proposal.proposal, "basis": basis, "src": src})
                if not out:
                    raise ValueError("nincs használható javaslat")
                return out

            try:
                return {"proposals": runner.ask(prompt, parse)}
            except OutputError as exc:
                return {"failed": str(exc)}

        for number, item in enumerate(batch):
            item["_id"] = f"{index}.{number}"
        part = runner.step(f"p:{index}", compute)
        if part.get("failed"):
            runner.warnings.append("A megoldási javaslatok egy részét nem sikerült elkészíteni.")
            continue
        proposals.extend(part["proposals"])
    return {"chunks": chunks, "items": items, "proposals": proposals}


# --- compare / gap -------------------------------------------------------------------------------------------------

_WORD = re.compile(r"\w+", re.UNICODE)


def stems(text: str) -> set[str]:
    """Rough word stems (first five letters of words of four or more): enough to match across Hungarian suffixes."""
    return {word[:5] for word in _WORD.findall(text.casefold()) if len(word) >= 4}


def jaccard(a: set[str], b: set[str]) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def containment(needle: set[str], haystack: set[str]) -> float:
    return len(needle & haystack) / len(needle) if needle else 0.0


def closest_chunk(text: str, chunks: Sequence[dict]) -> tuple[dict | None, float]:
    """The chunk of the other document that contains most of this statement's words."""
    needle = stems(text)
    best, score = None, 0.0
    for chunk in chunks:
        value = containment(needle, stems(chunk["text"]))
        if value > score:
            best, score = chunk, value
    return best, score


def candidates_for(rows_a: list[dict], rows_b: list[dict], chunks_b: Sequence[dict], chunks_a: Sequence[dict], gap: bool) -> list[dict]:
    """Statements worth a model's judgement: paired ones that differ, and ones without a counterpart.

    Deterministic, so the person can trust what was not flagged: a statement matching its counterpart almost
    word for word is never reported as a difference.
    """
    stems_a = [stems(row["text"]) for row in rows_a]
    stems_b = [stems(row["text"]) for row in rows_b]
    found: list[dict] = []
    matched_b: set[int] = set()
    for i, row in enumerate(rows_a):
        scores = [jaccard(stems_a[i], s) for s in stems_b]
        best = max(range(len(scores)), key=scores.__getitem__) if scores else None
        if best is not None and scores[best] >= ROW_SAME:
            matched_b.add(best)
            continue
        if best is not None and scores[best] >= ROW_RELATED:
            matched_b.add(best)
            # Also in gap mode: a requirement B only partly repeats (another threshold, another deadline) is not
            # covered; the judge decides, and an unjudged pair stays on the list as uncertain.
            found.append({"kind": "changed", "a": row, "b": rows_b[best]})
            continue
        chunk, score = closest_chunk(row["text"], chunks_b)
        found.append({
            "kind": "missing_in_b", "a": row, "b": None,
            "absent": score < ABSENT_BELOW,
            "closest": chunk if chunk is not None and score >= CLOSEST_FROM else None,
        })
    if not gap:
        for j, row in enumerate(rows_b):
            if j in matched_b or any(jaccard(stems_b[j], s) >= ROW_RELATED for s in stems_a):
                continue
            chunk, score = closest_chunk(row["text"], chunks_a)
            found.append({
                "kind": "only_in_b", "a": None, "b": row,
                "absent": score < ABSENT_BELOW,
                "closest": chunk if chunk is not None and score >= CLOSEST_FROM else None,
            })
    return found


def run_compare(runner: Runner, documents: list[dict], options: dict) -> dict:
    if len(documents) != 2:
        raise JobFailed("Az összehasonlításhoz pontosan két dokumentum kell.")
    gap = options.get("mode") == "gap"
    types = [t for t in options.get("types") or COMPARE_TYPES if t in EXTRACT_TYPES and t != "table"] or list(COMPARE_TYPES)
    types_text = "\n".join(f"- {t} ({TYPE_LABELS[t]})" for t in types)
    doc_a, doc_b = documents
    windows_a = doc_text.make_windows(doc_a["chunks"], runner.window_chars)
    windows_b = doc_text.make_windows(doc_b["chunks"], runner.window_chars)
    runner.plan(len(windows_a) + len(windows_b), "Állítások kigyűjtése")
    template = runner.prompts("extract")
    rows = []
    for side, windows in (("a", windows_a), ("b", windows_b)):
        rows.append(_dedupe(map_windows(
            runner, f"c{side}", windows, template, key="items", model=ItemModel, allowed=types,
            values={"types": types_text},
        )))
    rows_a, rows_b = rows
    found = candidates_for(rows_a, rows_b, doc_b["chunks"], doc_a["chunks"], gap)
    for number, candidate in enumerate(found):
        candidate["_n"] = number

    def describe(candidate: dict) -> str:
        parts = [f"fajta: {candidate['kind']}"]
        if candidate["a"]:
            parts.append(f"A: {candidate['a']['text']}")
        if candidate["b"]:
            parts.append(f"B: {candidate['b']['text']}")
        if candidate.get("closest"):
            parts.append(f"closest: {candidate['closest']['text'][:500]}")
        return " | ".join(parts)

    batches = _batches(found, describe, runner.window_chars, per_batch=JUDGE_BATCH)
    runner.plan(len(batches), "Eltérések értékelése")
    verdicts: dict[int, dict] = {}
    for index, batch in enumerate(batches):

        def compute(batch=batch) -> dict:
            prompt = render(runner.prompts("compare"), context=_numbered(batch, describe))

            def parse(text: str) -> dict[int, dict]:
                out: dict[int, dict] = {}
                for raw in _json_list(text, "verdicts"):
                    try:
                        verdict = VerdictModel.model_validate(raw)
                    except ValidationError:
                        continue
                    # An invented candidate number is dropped, like an invented citation.
                    if 1 <= verdict.candidate <= len(batch):
                        out[batch[verdict.candidate - 1]["_n"]] = verdict.model_dump()
                if raw_count(text) and not out:
                    raise ValueError("egyetlen ítélet sem hivatkozott érvényes jelöltre")
                return out

            try:
                return {"verdicts": {str(k): v for k, v in runner.ask(prompt, parse).items()}}
            except OutputError:
                return {"verdicts": {}, "unjudged": True}

        part = runner.step(f"j:{index}", compute)
        verdicts.update({int(k): v for k, v in part["verdicts"].items()})
        if part.get("unjudged"):
            runner.warnings.append("A modell nem értékelte az eltérések egy részét; azok bizonytalanként szerepelnek.")
    findings = []
    for candidate in found:
        verdict = verdicts.get(candidate["_n"])
        if verdict and verdict["verdict"].strip().lower() == "equivalent":
            continue
        findings.append(_finding(candidate, verdict))
    return {"chunks": doc_a["chunks"] + doc_b["chunks"], "findings": findings, "mode": "gap" if gap else "diff",
            "documents": [doc_a["file_name"], doc_b["file_name"]]}


def raw_count(text: str) -> int:
    try:
        return len(_json_list(text, "verdicts"))
    except ValueError:
        return 0


def _finding(candidate: dict, verdict: dict | None) -> dict:
    """One difference with its evidence. ``evidenced`` only when the claim can be checked against the sources:
    both statements exist for a change; for a missing one, the other document's whole text holds nothing close."""
    kind = candidate["kind"]
    judged = bool(verdict and verdict["verdict"].strip().lower() == "different")
    if kind == "changed":
        status, reason = ("evidenced", "Mindkét dokumentumban megvan a megfelelő állítás.") if judged else (
            "uncertain", "A modell nem erősítette meg az eltérést.")
    elif judged and candidate["absent"]:
        status, reason = "evidenced", "A másik dokumentum teljes szövegében nincs hasonló részlet."
    elif judged:
        status, reason = "uncertain", "A másik dokumentumban van hasonló részlet; lehet, hogy más szavakkal szerepel."
    else:
        status, reason = "uncertain", "A modell nem erősítette meg a hiányt."
    default = {
        "changed": "A két dokumentum hasonló állítása eltér.",
        "missing_in_b": "Az A dokumentum állítása a B-ben nem található.",
        "only_in_b": "A B dokumentum állítása az A-ban nem található.",
    }[kind]
    return {
        "kind": kind,
        "description": (verdict or {}).get("description") or default,
        "status": status,
        "reason": reason,
        "a": candidate["a"],
        "b": candidate["b"],
        "closest": candidate.get("closest") and {"text": candidate["closest"]["text"][:400],
                                                  "src": [candidate["closest"]["chunk_id"]]},
    }


# --- translation ---------------------------------------------------------------------------------------------------


def clean_translation(text: str, source: str) -> str:
    out = re.sub(r"^```\w*\s*|\s*```$", "", text.strip()).strip()
    if not out:
        raise ValueError("üres fordítás")
    low, high = TRANSLATION_RATIO
    if not low * len(source) <= len(out) <= high * len(source) + 200:
        raise ValueError("a fordítás hossza a szöveghez képest hihetetlen")
    return out


def run_translate(runner: Runner, documents: list[dict], options: dict) -> dict:
    direction = options.get("direction")
    if direction not in TRANSLATE_DIRECTIONS:
        raise JobFailed("A fordítás iránya hu-en vagy en-hu lehet.")
    template = runner.prompts(f"translate_{direction}")
    chunks: list[dict] = []
    segments: list[dict] = []
    plan = [(document, doc_text.make_windows(document["chunks"], runner.translate_chars)) for document in documents]
    runner.plan(sum(len(w) for _, w in plan), "Fordítás")
    for number, (document, windows) in enumerate(plan):
        chunks.extend(document["chunks"])
        runner.windows_total += len(windows)
        for index, window in enumerate(windows):
            source_text = "\n\n".join(chunk["text"] for chunk in window)

            def compute(source_text=source_text, window=window) -> dict:
                try:
                    return {"text": runner.ask(
                        render(template, text=source_text), lambda text: clean_translation(text, source_text),
                        json_mode=False)}
                except OutputError as exc:
                    return {"failed": str(exc), "place": _failure_place(window)}

            part = runner.step(f"x{number}:{index}", compute)
            if part.get("failed"):
                # A hole in a translation is worse than no translation: the job fails and can be resumed.
                raise JobFailed(f"A fordítás nem sikerült ({part['place']}): {part['failed']}. Újrapróbálható.")
            segments.append({"text": part["text"], "src": [c["chunk_id"] for c in window],
                             "label": _failure_place(window), "file_name": document["file_name"]})
    return {"chunks": chunks, "segments": segments, "direction": direction}


# --- result --------------------------------------------------------------------------------------------------------

RUNNERS: dict[str, Callable[[Runner, list[dict], dict], dict]] = {
    "summary": run_summary,
    "extract": run_extract,
    "compare": run_compare,
    "tasks": run_tasks,
    "translate": run_translate,
}
# More than this share of windows given up on, and the job fails instead of presenting a mostly empty result.
MAX_FAILED_SHARE = 0.5


def _ids(src: Sequence[str], id_of: dict[str, int]) -> list[int]:
    return sorted({id_of[chunk_id] for chunk_id in src if chunk_id in id_of})


def finalize(kind: str, raw: dict, runner: Runner, documents: list[dict], limits: citations.Limits) -> dict:
    """Number the cited chunks 1..N, turn ``src`` into citation ids and rate the result with ``citations.assess``."""
    if runner.windows_total and runner.failed_places and len(runner.failed_places) / runner.windows_total > MAX_FAILED_SHARE:
        raise JobFailed(
            f"A modell a dokumentum {len(runner.failed_places)}/{runner.windows_total} részére nem adott használható "
            "kimenetet, ezért nincs megbízható eredmény. Próbálj kisebb dokumentumot vagy másik modellt."
        )
    chunks_by_id = {chunk["chunk_id"]: chunk for chunk in raw["chunks"]}
    cited: set[str] = set()

    def collect(src: Sequence[str]) -> None:
        cited.update(s for s in src if s in chunks_by_id)

    result: dict[str, Any] = {}
    if kind == "summary":
        for entry in raw["overall"]:
            collect(entry["src"])
        for section in raw["sections"]:
            for entry in section["points"]:
                collect(entry["src"])
    elif kind in ("extract", "tasks"):
        for item in raw["items"]:
            collect(item["src"])
        for proposal in raw.get("proposals", []):
            collect(proposal["src"])
    elif kind == "compare":
        for finding in raw["findings"]:
            for side in ("a", "b", "closest"):
                if finding[side]:
                    collect(finding[side]["src"])
    else:
        for segment in raw["segments"]:
            collect(segment["src"])

    order = {chunk["chunk_id"]: n for n, chunk in enumerate(raw["chunks"])}
    hits = sorted((chunks_by_id[c] for c in cited), key=lambda chunk: order[chunk["chunk_id"]])
    built = citations.build_citations(hits, limits)
    id_of = {citation["chunk_id"]: citation["id"] for citation in built}
    uncertain = {citation["id"] for citation in built if citation["uncertain"]}

    def point(item: dict) -> dict:
        ids = _ids(item["src"], id_of)
        return {"text": item["text"], "sources": ids, "uncertain": bool(set(ids) & uncertain)}

    # What `assess` reads: every statement followed by its source numbers, or the "no source" marker.
    statements: list[str] = []

    def mark(text: str, ids: Sequence[int]) -> None:
        statements.append(f"{text} [{', '.join(map(str, ids))}]" if ids else f"{text} {citations.UNSOURCED_MARKER}")

    if kind == "summary":
        result["overall"] = [point(p) for p in raw["overall"]]
        result["sections"] = [
            {"label": s["label"], "file_name": s["file_name"], "points": [point(p) for p in s["points"]]}
            for s in raw["sections"]
        ]
        for p in result["overall"] + [p for s in result["sections"] for p in s["points"]]:
            mark(p["text"], p["sources"])
    elif kind in ("extract", "tasks"):
        rows = []
        for number, item in enumerate(raw["items"], start=1):
            row = {"id": number, "type": item["type"], "type_label": TYPE_LABELS.get(item["type"], item["type"]),
                   "detail": item.get("detail") or "", **point(item)}
            if item.get("table"):
                row["table"] = item["table"]
            rows.append(row)
            mark(row["text"], row["sources"])
        result["rows"] = rows
        if kind == "extract":
            result["types"] = raw["types"]
        else:
            id_by_ref = {item.get("_id"): number for number, item in enumerate(raw["items"], start=1)}
            conclusions = []
            for number, proposal in enumerate(raw["proposals"], start=1):
                ids = _ids(proposal["src"], id_of)
                # A conclusion is the model's own; "grounded_in" names the source rows it rests on, if any.
                conclusions.append({"id": number, "text": proposal["text"], "grounded_in": [
                    id_by_ref[b] for b in proposal["basis"] if b in id_by_ref], "sources": ids, "own": True})
            result["conclusions"] = conclusions
            # Unlike the source data, a conclusion is never "grounded" in a document: it is rated as the model's own.
            for c in conclusions:
                mark(c["text"], [])
    elif kind == "compare":
        findings = []
        for number, finding in enumerate(raw["findings"], start=1):
            entry = {"id": number, "kind": finding["kind"], "description": finding["description"],
                     "status": finding["status"], "reason": finding["reason"]}
            for side in ("a", "b"):
                entry[side] = point(finding[side]) if finding[side] else None
            entry["closest"] = (
                {"text": finding["closest"]["text"], "sources": _ids(finding["closest"]["src"], id_of)}
                if finding["closest"] else None)
            findings.append(entry)
            mark(entry["description"], sorted({s for side in ("a", "b") if entry[side] for s in entry[side]["sources"]}))
        result["findings"] = findings
        result["mode"] = raw["mode"]
        result["documents"] = raw["documents"]
    else:
        result["direction"] = raw["direction"]
        result["segments"] = [
            {"text": s["text"], "label": s["label"], "file_name": s["file_name"], "sources": _ids(s["src"], id_of)}
            for s in raw["segments"]
        ]
        for s in result["segments"]:
            mark(s["label"], s["sources"])

    grounding = citations.assess("\n".join(statements), built)
    notice = [grounding["notice"]] if grounding["notice"] and kind not in ("translate", "tasks") else []
    if not statements:
        notice = ["A dokumentumból nem sikerült egyetlen elemet sem kigyűjteni."]
    elif kind == "tasks":
        notice.append("A „Következtetések” a modell saját javaslatai, nem a dokumentum állításai.")
    if runner.failed_places:
        notice.append("Ezeket a részeket nem sikerült feldolgozni, az eredmény nem teljes: " + "; ".join(runner.failed_places[:20]))
    notice.extend(dict.fromkeys(runner.warnings))
    if runner.rejected:
        notice.append(f"{runner.rejected} elemet elvetettem, mert nem hivatkozott érvényes forrásra.")
    result.update({
        "kind": kind,
        "citations": built,
        "grounding": grounding,
        "notice": " ".join(notice) or None,
        "coverage": {"windows": runner.windows_total, "failed": len(runner.failed_places),
                     "failed_places": runner.failed_places, "rejected_items": runner.rejected},
        "documents_used": [{"source_path": d["source_path"], "file_name": d["file_name"], "title": d["title"]}
                           for d in documents],
    })
    return result


def run_job(kind: str, runner: Runner, documents: list[dict], options: dict) -> dict:
    """Run one job to its result. Raises ``JobCancelled`` or ``JobFailed``; the model failing is a JobFailed too."""
    if kind not in RUNNERS:
        raise JobFailed("Ismeretlen feladattípus.")
    if not any(document["chunks"] for document in documents):
        raise JobFailed("A dokumentumnak nincs feldolgozható szövege.")
    raw = RUNNERS[kind](runner, documents, options)
    return finalize(kind, raw, runner, documents, runner.limits)


# --- export --------------------------------------------------------------------------------------------------------


def _label(result: dict, ids: Sequence[int]) -> str:
    by_id = {c["id"]: c for c in result["citations"]}
    return "; ".join(by_id[i]["label"] for i in ids if i in by_id)


def _csv_safe(value: Any) -> str:
    """A spreadsheet runs a cell that starts with = + - @ as a formula; prefix those so a document cannot."""
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


def table_view(result: dict) -> tuple[list[str], list[list[str]]]:
    """The result of any non-translation kind as one flat table: (header, rows). Every row has its source."""
    kind = result["kind"]
    if kind == "extract":
        header = ["Típus", "Tartalom", "Részlet", "Forrás (fájl, oldal/fejezet)", "Bizonytalan"]
        rows = []
        for row in result["rows"]:
            detail = row["detail"]
            if row.get("table"):
                cols = row["table"]["columns"]
                detail = " / ".join("; ".join(f"{c}={v}" for c, v in zip(cols, r, strict=False)) for r in row["table"]["rows"])
            rows.append([row["type_label"], row["text"], detail, _label(result, row["sources"]),
                         "igen" if row["uncertain"] else ""])
        return header, rows
    if kind == "tasks":
        header = ["Rész", "Típus", "Tartalom", "Részlet", "Forrás (fájl, oldal/fejezet)"]
        rows = [["Forrásadat", r["type_label"], r["text"], r["detail"], _label(result, r["sources"])] for r in result["rows"]]
        by_id = {r["id"]: r for r in result["rows"]}
        for c in result["conclusions"]:
            basis = ", ".join(by_id[i]["text"][:60] for i in c["grounded_in"] if i in by_id)
            rows.append(["Következtetés (a modell javaslata)", "javaslat", c["text"], basis, _label(result, c["sources"])])
        return header, rows
    if kind == "compare":
        names = result["documents"]
        header = ["Fajta", "Leírás", "Státusz", f"A: {names[0]}", "A forrása", f"B: {names[1]}", "B forrása"]
        kinds = {"changed": "eltérés", "missing_in_b": "hiányzik a B-ből", "only_in_b": "csak a B-ben van"}
        status = {"evidenced": "bizonyított", "uncertain": "bizonytalan"}
        rows = [[kinds[f["kind"]], f["description"], status[f["status"]],
                 f["a"]["text"] if f["a"] else "", _label(result, f["a"]["sources"]) if f["a"] else "",
                 f["b"]["text"] if f["b"] else "", _label(result, f["b"]["sources"]) if f["b"] else ""]
                for f in result["findings"]]
        return header, rows
    # summary
    header = ["Rész", "Pont", "Forrás (fájl, oldal/fejezet)"]
    rows = [["Összefoglaló", p["text"], _label(result, p["sources"])] for p in result["overall"]]
    for section in result["sections"]:
        rows.extend([[section["label"], p["text"], _label(result, p["sources"])] for p in section["points"]])
    return header, rows


def export_csv(result: dict) -> str:
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\r\n")
    header, rows = table_view(result)
    writer.writerow([_csv_safe(h) for h in header])
    for row in rows:
        writer.writerow([_csv_safe(cell) for cell in row])
    # The BOM makes Excel read the accented text as UTF-8.
    return "\ufeff" + out.getvalue()


def _md_cell(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def export_markdown(result: dict, title: str) -> str:
    lines = [f"# {title}", ""]
    if result.get("notice"):
        lines += [f"> {result['notice']}", ""]
    if result["kind"] == "translate":
        for segment in result["segments"]:
            lines += [f"<!-- {segment['label']} -->", "", segment["text"], ""]
    else:
        header, rows = table_view(result)
        lines.append("| " + " | ".join(header) + " |")
        lines.append("|" + "---|" * len(header))
        lines += ["| " + " | ".join(_md_cell(cell) for cell in row) + " |" for row in rows]
        if result["kind"] == "extract":
            for row in result["rows"]:
                if row.get("table"):
                    table = row["table"]
                    lines += ["", f"**{row['text']}** ({_label(result, row['sources'])})", "",
                              "| " + " | ".join(_md_cell(c) for c in table["columns"]) + " |",
                              "|" + "---|" * len(table["columns"])]
                    lines += ["| " + " | ".join(_md_cell(c) for c in r) + " |" for r in table["rows"]]
    lines += ["", "## Források", ""]
    lines += [f"{c['id']}. {c['label']}" + (" (bizonytalan)" if c["uncertain"] else "") for c in result["citations"]]
    return "\n".join(lines) + "\n"


def export_text(result: dict) -> str:
    if result["kind"] != "translate":
        raise ValueError("csak fordítás exportálható szövegként")
    return "\n\n".join(segment["text"] for segment in result["segments"]) + "\n"


def load_prompt(prompt_dir: Path | str, name: str) -> str:
    return (Path(prompt_dir) / PROMPTS[name]).read_text(encoding="utf-8")
