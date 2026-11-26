"""Build and export meeting notes grounded in transcript segment IDs."""

import re
from collections.abc import Iterable, Iterator
from functools import partial

from teacher_common.docintel import OutputError, Runner, render
from teacher_common.study import extract_json

SECTIONS = {
    "summary": "Összefoglaló",
    "decisions": "Döntések",
    "questions": "Nyitott kérdések",
    "actions": "Teendők",
}


def _parse(raw: str, allowed: set[int], keys: Iterable[str]) -> dict:
    """The answer's items, each with only real segment IDs from its window; an item without one is dropped."""
    data = extract_json(raw)
    if not isinstance(data, dict):
        raise ValueError("JSON objektum szükséges")
    result: dict[str, list[dict]] = {}
    for key in keys:
        items = data.get(key)
        if not isinstance(items, list):
            raise ValueError(f"hiányzó tömb: {key}")
        result[key] = []
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get("text"), str):
                raise ValueError("szöveges megállapítás szükséges")
            text = " ".join(item["text"].split())
            sources = item.get("segments")
            ids = sorted({s for s in sources if type(s) is int and s in allowed})[:8] if isinstance(sources, list) else []
            # Ungrounded (no real source) or empty: not shown, rather than shown without a timestamp to check.
            if text and ids:
                result[key].append({"text": text[:300], "segments": ids})
        if key == "summary":
            result[key] = result[key][:3]
    return result


def _windows(segments: list[dict], budget: int) -> Iterator[list[tuple[int, str]]]:
    window: list[tuple[int, str]] = []
    size = 0
    for segment in segments:
        prefix = f"[{segment['id']}] {timestamp(segment['start'])} "
        text = segment["text"]
        width = budget - len(prefix) - 1
        for start in range(0, len(text), width):
            line = prefix + text[start:start + width]
            if size + len(line) + 1 > budget:
                yield window
                window, size = [], 0
            window.append((segment["id"], line))
            size += len(line) + 1
    if window:
        yield window


def _deduplicate(items: list[dict]) -> list[dict]:
    unique: dict[str, dict] = {}
    for item in items:
        existing = unique.get(item["text"].casefold())
        if existing is None:
            unique[item["text"].casefold()] = item
        else:
            existing["segments"] = sorted(set(existing["segments"]) | set(item["segments"]))[:8]
    return list(unique.values())


def _map_window(runner: Runner, window: list[tuple[int, str]]) -> dict:
    prompt = render(runner.prompts("map"), context="\n".join(line for _, line in window))
    allowed = {idx for idx, _ in window}
    try:
        return runner.ask(prompt, lambda answer: _parse(answer, allowed, SECTIONS))
    except OutputError:
        # One unusable window must not lose a long meeting's notes: it is reported as a gap instead.
        return {"failed": True}


def _reduce_batch(runner: Runner, batch: list[dict]) -> list[dict]:
    if len(batch) <= 3:
        return batch
    valid = {idx for item in batch for idx in item["segments"]}
    context = "\n".join(f"{item['segments']} {item['text']}" for item in batch)
    prompt = render(runner.prompts("reduce"), context=context)
    try:
        reduced = runner.ask(prompt, lambda answer: _parse(answer, valid, ("summary",))["summary"])
    except OutputError:
        reduced = []
    return reduced or batch[:3]


def summarize(segments: list[dict], runner: Runner) -> dict:
    """Map each transcript window to grounded notes, then reduce the summary points to at most three."""
    notes: dict[str, list] = {key: [] for key in SECTIONS}
    gaps: list[dict] = []
    starts = {segment["id"]: segment for segment in segments}
    windows = list(_windows(segments, min(4800, max(1200, runner.window_chars))))
    runner.plan(len(windows), "summarizing")
    for index, window in enumerate(windows):
        batch = runner.step(f"map-{index}", partial(_map_window, runner, window))
        if batch.get("failed"):
            gaps.append({"start": starts[window[0][0]]["start"], "end": starts[window[-1][0]]["end"]})
            continue
        for key in SECTIONS:
            notes[key].extend(batch[key])
    if windows and len(gaps) == len(windows):
        raise OutputError("egyik részlethez sem készült használható jegyzet")

    notes = {key: _deduplicate(items) for key, items in notes.items()}
    level = 0
    while len(notes["summary"]) > 3:
        items = notes["summary"]
        batches = [items[i:i + 8] for i in range(0, len(items), 8)]
        runner.plan(len(batches), "summarizing")
        notes["summary"] = [
            point for index, batch in enumerate(batches)
            for point in runner.step(f"reduce-{level}-{index}", partial(_reduce_batch, runner, batch))
        ]
        level += 1
    notes["gaps"] = gaps
    return notes


def timestamp(seconds: float, separator: str = ".") -> str:
    seconds, millis = divmod(round(seconds * 1000), 1000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}{separator}{millis:03d}"


def export_transcript(segments: list[dict], *, srt: bool) -> str:
    if srt:
        return "".join(
            f"{index}\n{timestamp(segment['start'], ',')} --> {timestamp(segment['end'], ',')}\n{segment['text']}\n\n"
            for index, segment in enumerate(segments, start=1)
        )
    return "".join(f"[{timestamp(s['start'])}] Ismeretlen beszélő — {s['text']}\n" for s in segments)


def _markdown_text(value: str) -> str:
    # Inline syntax anywhere, and block syntax at the start ("# x", "1. x" would nest inside the list item).
    text = re.sub(r"([\\`*_\[\]<>|~])", r"\\\1", " ".join(value.split()))
    text = re.sub(r"^(\d+)([.)])", r"\1\\\2", text)
    return re.sub(r"^([#+-])", r"\\\1", text)


def export_notes(filename: str, segments: list[dict], notes: dict) -> str:
    times = {s["id"]: timestamp(s["start"]) for s in segments}
    lines = [f"# {_markdown_text(filename)}", ""]
    for gap in notes.get("gaps") or []:
        lines.extend([f"> Ehhez a részhez nem készült jegyzet: {timestamp(gap['start'])} – {timestamp(gap['end'])}", ""])
    for key, label in SECTIONS.items():
        lines.extend([f"## {label}", ""])
        for item in notes[key]:
            name = item.get("assignee")
            source = ", ".join(times[idx] for idx in item["segments"])
            owner = f" — Felelős: {_markdown_text(name)}" if name else ""
            lines.append(f"- {_markdown_text(item['text'])}{owner} ({source})")
        if not notes[key]:
            lines.append("Nincs ilyen bejegyzés.")
        lines.append("")
    return "\n".join(lines)
