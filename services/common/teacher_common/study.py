"""Generation, parsing/validation and spaced-repetition logic for quizzes and flashcards.

Pure functions only (no database, no HTTP), so they are easy to test with a mocked LLM.
"""

import json
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

LLM = Callable[[str], str]

SOURCE_KEYS = ("chunk_id", "file_id", "file_name", "source_path", "title", "locator", "page", "chapter")


class GenerationError(Exception):
    """The model did not produce usable output within the allowed attempts."""


# --- validated output schemas -------------------------------------------------


class _Item(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    sources: list[int] = Field(min_length=1)

    @field_validator("sources", mode="before")
    @classmethod
    def _coerce_sources(cls, value: Any) -> Any:
        if isinstance(value, int):
            return [value]
        if isinstance(value, list):
            return [int(v) if isinstance(v, str) and v.strip().isdigit() else v for v in value]
        return value


class QuizItem(_Item):
    question: str = Field(min_length=5)
    options: list[str] = Field(min_length=2, max_length=6)
    correct_index: int
    explanation: str = ""

    @field_validator("options")
    @classmethod
    def _options_valid(cls, value: list[str]) -> list[str]:
        value = [v.strip() for v in value if isinstance(v, str)]
        if any(not v for v in value):
            raise ValueError("empty option")
        if len({v.casefold() for v in value}) != len(value):
            raise ValueError("duplicate options")
        return value

    @model_validator(mode="after")
    def _index_in_range(self) -> "QuizItem":
        if not 0 <= self.correct_index < len(self.options):
            raise ValueError("correct_index out of range")
        return self


class CardItem(_Item):
    front: str = Field(min_length=2)
    back: str = Field(min_length=1)


@dataclass
class ParseResult:
    items: list[dict]
    rejected: list[str]


# --- prompt building ----------------------------------------------------------


def load_prompt(prompt_dir: Path | str, kind: str) -> str:
    name = {"quiz": "quiz_hu.md", "flashcards": "flashcards_hu.md"}[kind]
    return (Path(prompt_dir) / name).read_text(encoding="utf-8")


def source_ref(hit: dict) -> dict:
    return {key: hit.get(key) for key in SOURCE_KEYS}


def build_context(hits: Sequence[dict], max_chars: int) -> tuple[str, list[dict]]:
    """Numbered context block and the hits that actually fit, in numbering order."""
    lines: list[str] = []
    used: list[dict] = []
    total = 0
    for hit in hits:
        title = hit.get("title") or Path(hit.get("source_path") or "").name or "ismeretlen dokumentum"
        line = f"[{len(used) + 1}] {title} | {hit.get('locator') or 'ismeretlen hely'} | {hit.get('text') or ''}"
        if used and total + len(line) > max_chars:
            break
        lines.append(line[: max_chars] if not used else line)
        total += len(line)
        used.append(hit)
    return "\n".join(lines), used


def render_prompt(template: str, *, level: str, count: int, topic: str, context: str) -> str:
    # str.replace rather than format(): the templates contain literal JSON braces.
    return (
        template.replace("{level}", level)
        .replace("{count}", str(count))
        .replace("{topic}", topic)
        .replace("{context}", context)
    )


# --- parsing ------------------------------------------------------------------

_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


def extract_json(text: str) -> Any:
    """Parse JSON from model output, tolerating code fences and surrounding prose."""
    cleaned = _FENCE.sub("", text.strip()).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    decoder = json.JSONDecoder()
    for match in re.finditer(r"[{\[]", cleaned):
        try:
            value, _ = decoder.raw_decode(cleaned[match.start():])
        except json.JSONDecodeError:
            continue
        return value
    raise ValueError("no JSON found in model output")


def _parse(text: str, key: str, schema: type[_Item], hits: Sequence[dict]) -> ParseResult:
    data = extract_json(text)
    if isinstance(data, dict):
        raw_items = data.get(key)
    else:
        raw_items = data
    if not isinstance(raw_items, list):
        raise ValueError(f'missing "{key}" list')

    items: list[dict] = []
    rejected: list[str] = []
    for index, raw in enumerate(raw_items, start=1):
        try:
            item = schema.model_validate(raw)
            # Map 1-based prompt numbers to real chunks; drop numbers the model invented.
            refs: list[dict] = []
            for number in dict.fromkeys(item.sources):
                if 1 <= number <= len(hits):
                    refs.append(source_ref(hits[number - 1]))
            if not refs:
                raise ValueError("no valid source reference")
            payload = item.model_dump(exclude={"sources"})
            payload["sources"] = refs
            items.append(payload)
        except (ValidationError, ValueError, TypeError) as exc:
            rejected.append(f"#{index}: {str(exc).splitlines()[0] if str(exc) else type(exc).__name__}")
    return ParseResult(items=items, rejected=rejected)


def parse_quiz(text: str, hits: Sequence[dict]) -> ParseResult:
    return _parse(text, "questions", QuizItem, hits)


def parse_flashcards(text: str, hits: Sequence[dict]) -> ParseResult:
    return _parse(text, "cards", CardItem, hits)


# --- generation with retry ----------------------------------------------------

_PARSERS = {"quiz": parse_quiz, "flashcards": parse_flashcards}


def generate_items(
    kind: str,
    *,
    llm: LLM,
    template: str,
    hits: Sequence[dict],
    level: str,
    count: int,
    topic: str,
    max_context_chars: int,
    max_attempts: int = 2,
) -> list[dict]:
    """Ask the model for ``count`` items; retry with feedback when the output is unusable.

    Returns validated items whose ``sources`` are real chunk references.
    """
    context, used = build_context(hits, max_context_chars)
    prompt = render_prompt(template, level=level, count=count, topic=topic, context=context)
    parser = _PARSERS[kind]
    last_problem = "ismeretlen hiba"
    for attempt in range(max_attempts):
        request = prompt
        if attempt:
            request += (
                "\n\nAz előző válaszod nem volt használható "
                f"({last_problem}). Add vissza újra, KIZÁRÓLAG a kért JSON szerkezetben."
            )
        raw = llm(request)
        try:
            result = parser(raw, used)
        except ValueError as exc:
            last_problem = str(exc)
            continue
        if result.items:
            return result.items[:count]
        last_problem = "; ".join(result.rejected[:3]) or "üres lista"
    raise GenerationError(f"A modell {max_attempts} próbálkozásra sem adott érvényes kimenetet: {last_problem}")


# --- spaced repetition (SM-2 style) --------------------------------------------

# UI grades -> SM-2 quality (0-5)
GRADES = {"again": 1, "hard": 3, "good": 4, "easy": 5}
MIN_EASE = 1.3
RELEARN_DELAY = timedelta(minutes=10)


@dataclass(frozen=True)
class ReviewState:
    ease: float
    interval_days: int
    repetitions: int
    lapses: int
    due_at: datetime


def initial_state(now: datetime) -> ReviewState:
    return ReviewState(ease=2.5, interval_days=0, repetitions=0, lapses=0, due_at=now)


def schedule(state: ReviewState, grade: str, now: datetime) -> ReviewState:
    """SM-2: failed cards restart (due again in minutes), passed cards grow by ease."""
    quality = GRADES[grade]
    ease = max(MIN_EASE, state.ease + 0.1 - (5 - quality) * (0.08 + (5 - quality) * 0.02))
    if quality < 3:
        return ReviewState(
            ease=max(MIN_EASE, state.ease - 0.2),
            interval_days=0,
            repetitions=0,
            lapses=state.lapses + 1,
            due_at=now + RELEARN_DELAY,
        )
    if state.repetitions == 0:
        interval = 1
    elif state.repetitions == 1:
        interval = 6
    else:
        interval = max(state.interval_days + 1, round(state.interval_days * ease))
    return ReviewState(
        ease=ease,
        interval_days=interval,
        repetitions=state.repetitions + 1,
        lapses=state.lapses,
        due_at=now + timedelta(days=interval),
    )
