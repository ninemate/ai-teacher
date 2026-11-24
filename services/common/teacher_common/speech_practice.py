"""Smart Talk (conversation practice) and Elevator Speech (pitch practice): scenarios, prompts, scoring sheets
and validation of the model's feedback. No I/O except reading prompt files; the router does the rest.

Feedback is only about what was said and how the conversation or pitch is built. The model's remarks must quote
the user's own words, and remarks whose quote is not in the transcript are dropped, so nothing is said about the
person that their words do not show.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from teacher_common import speech_metrics as sm

LANGUAGE_NAMES = {"hu": "magyar", "en": "angol"}

# --- Smart Talk ---------------------------------------------------------------------------------------------------

TALK_SKILLS: dict[str, str] = {
    "opening": "Nyitás",
    "sustaining": "A beszélgetés fenntartása",
    "topic_switch": "Témaváltás",
    "open_questions": "Nyitott kérdések",
    "listening": "Aktív figyelem",
    "clarity": "Érthetőség",
    "closing": "Természetes lezárás",
}


@dataclass(frozen=True)
class TalkScenario:
    id: str
    category: str  # everyday | professional | social
    title: str
    setting: str  # Hungarian description of the situation, for the user and the model
    partner: str  # who the partner is
    topics: tuple[str, ...]  # words that belong to the situation (topic overlap)
    partner_starts: bool = False


TALK_CATEGORIES = {"everyday": "Hétköznapi", "professional": "Szakmai", "social": "Társasági"}

TALK_SCENARIOS: tuple[TalkScenario, ...] = (
    TalkScenario(
        "neighbour", "everyday", "Új szomszéd a lépcsőházban",
        "A lépcsőházban összefutsz az új szomszéddal, aki éppen beköltözik. Pár perced van.",
        "az új szomszéd, aki most költözik be", ("költözés", "ház", "környék", "bolt", "park", "munka"),
    ),
    TalkScenario(
        "queue", "everyday", "Sorban állás a kávézóban",
        "Hosszú a sor a kávézóban, a melletted álló vendég megjegyzést tesz a várakozásra.",
        "egy vendég a sorban", ("kávé", "sor", "reggel", "munka", "hétvége", "környék"), partner_starts=True,
    ),
    TalkScenario(
        "networking", "professional", "Kávészünet egy szakmai konferencián",
        "Szakmai konferencia kávészünetében állsz egy ismeretlen résztvevő mellett.",
        "egy másik cég szakembere", ("előadás", "konferencia", "projekt", "cég", "szakma", "tapasztalat"),
    ),
    TalkScenario(
        "new_colleague", "professional", "Új kolléga az első héten",
        "Az irodai konyhában találkozol egy új kollégával, aki most kezdett a csapatban.",
        "az új kolléga", ("csapat", "projekt", "iroda", "munka", "korábban", "beilleszkedés"),
    ),
    TalkScenario(
        "client_smalltalk", "professional", "Ügyfél a megbeszélés előtt",
        "Egy ügyfél korábban érkezett a megbeszélésre; pár perc kötetlen beszélgetés a tárgyalás előtt.",
        "az ügyfél képviselője", ("utazás", "cég", "piac", "projekt", "időjárás", "megbeszélés"),
        partner_starts=True,
    ),
    TalkScenario(
        "party", "social", "Házibuli, ahol csak a házigazdát ismered",
        "Egy házibuliban vagy, a házigazdán kívül senkit sem ismersz. Valaki egyedül áll az asztal mellett.",
        "egy vendég, a házigazda régi ismerőse", ("házigazda", "zene", "hobbi", "utazás", "film", "munka"),
    ),
    TalkScenario(
        "wedding", "social", "Asztaltárs egy esküvői vacsorán",
        "Egy esküvői vacsorán ismeretlen asztaltárs mellé ültettek.",
        "egy asztaltárs a vőlegény családjából", ("esküvő", "pár", "család", "étel", "zene", "utazás"),
    ),
)

TALK_STYLES: dict[str, dict[str, str]] = {
    "friendly": {
        "label": "Barátságos, beszédes",
        "prompt": "Barátságos és nyitott vagy, szívesen beszélsz, és néha visszakérdezel.",
    },
    "reserved": {
        "label": "Szűkszavú",
        "prompt": "Udvarias, de szűkszavú vagy: rövid válaszokat adsz, ritkán kérdezel vissza, és nem viszed a "
        "beszélgetést. Ha jó nyitott kérdést kapsz, kicsit bővebben válaszolsz.",
    },
    "busy": {
        "label": "Sietős",
        "prompt": "Siettél valahová, kedves vagy, de néhány váltás után jelzed, hogy indulnod kell. Figyeld, "
        "hogyan zárja le a másik a beszélgetést.",
    },
    "dominant": {
        "label": "Sokat beszélő",
        "prompt": "Sokat és szívesen beszélsz magadról, hosszabb válaszokat adsz, néha témát váltasz. A másiknak "
        "kell bekapcsolódnia és irányítania.",
    },
    "skeptical": {
        "label": "Tartózkodó, óvatos",
        "prompt": "Óvatos és kissé tartózkodó vagy, eleinte rövid válaszokat adsz, és csak akkor nyílsz meg, ha "
        "a másik érdeklődése őszintének tűnik.",
    },
}

MAX_TALK_TURNS = 30  # the user's turns per conversation
MAX_TURN_CHARS = 4000


def talk_scenario(scenario_id: str) -> TalkScenario | None:
    return next((s for s in TALK_SCENARIOS if s.id == scenario_id), None)


def transcript_lines(turns: Sequence[Mapping], *, user: str = "Felhasználó", partner: str = "Partner") -> str:
    return "\n".join(
        f"{user if turn.get('role') == 'user' else partner}: {turn.get('text', '')}" for turn in turns
    )


def suggest_focus(metrics: Mapping) -> str:
    """The skill a targeted retry should practise, from the measured metrics alone (used when the model's
    choice is missing or unusable)."""
    if metrics.get("turns", 0) == 0:
        return "opening"
    if metrics.get("monologues", 0) > 0 or metrics.get("talk_share", 0) > 0.7:
        return "sustaining"
    if metrics.get("questions", 0) == 0 or metrics.get("question_turn_ratio", 0) < 0.25:
        return "open_questions"
    if metrics.get("open_question_ratio", 0) < 0.5:
        return "open_questions"
    if metrics.get("listening_replies", 0) and metrics.get("listening_ratio", 0) < 0.4:
        return "listening"
    if metrics.get("fillers_per_100_words", 0) > 6 or metrics.get("repetition", 0) > 0.2:
        return "clarity"
    return "closing"


# --- Elevator Speech ----------------------------------------------------------------------------------------------

PITCH_TARGETS = (30, 60, 90)
PITCH_PARTS: dict[str, str] = {
    "opening": "Nyitás",
    "value": "Értékajánlat",
    "evidence": "Bizonyíték",
    "close": "Zárás",
}


@dataclass(frozen=True)
class PitchScenario:
    id: str
    title: str
    listener: str  # who listens, for the model
    hint: str  # what a good pitch here does, for the user


PITCH_SCENARIOS: tuple[PitchScenario, ...] = (
    PitchScenario(
        "job_interview", "Állásinterjú: mutatkozz be!",
        "az állásinterjú kérdezője, aki most kérte: mutatkozzon be röviden",
        "Ki vagy, mit hozol a pozícióba, egy konkrét eredmény, és miért éppen ez a hely.",
    ),
    PitchScenario(
        "management", "Vezetői megbeszélés: javaslat",
        "a vezetői megbeszélés résztvevői, akiknek kevés idejük van egy új javaslatra",
        "Mi a probléma vagy lehetőség, mit javasolsz, mivel igazolod, és mit kérsz a vezetéstől.",
    ),
    PitchScenario(
        "client", "Ügyfélbemutatkozás",
        "egy lehetséges ügyfél, aki most hall először rólad és a szolgáltatásodról",
        "Kinek segítesz, miben, egy kézzelfogható példa vagy eredmény, és mi legyen a következő lépés.",
    ),
)

INTEREST_LEVELS: dict[str, dict[str, str]] = {
    "low": {
        "label": "Kevéssé érdeklődő",
        "prompt": "Kevéssé érdekel a téma, kételkedő és türelmetlen vagy: rövid, kritikus, a lényegre kérdező "
        "kérdéseket teszel fel (miért fontos ez nekem, mi a bizonyíték, mennyibe kerül).",
    },
    "medium": {
        "label": "Semleges",
        "prompt": "Tárgyszerű és semleges vagy: tisztázó kérdéseket teszel fel arról, ami homályos maradt.",
    },
    "high": {
        "label": "Nagyon érdeklődő",
        "prompt": "Nagyon érdekel a téma: részletekre, következő lépésekre és konkrét példákra kérdezel rá.",
    },
}

MAX_PITCH_CHARS = 6000
MAX_KEY_MESSAGES = 5


def pitch_scenario(scenario_id: str) -> PitchScenario | None:
    return next((s for s in PITCH_SCENARIOS if s.id == scenario_id), None)


def _clamp(value: float) -> int:
    return int(round(min(100.0, max(0.0, value))))


def time_score(duration: float | None, target: int) -> int | None:
    """100 between 80% and 100% of the target; falls off faster for overrunning than for being short."""
    if not duration:
        return None
    ratio = duration / target
    if ratio > 1.0:
        return _clamp(100 - (ratio - 1.0) * 300)
    if ratio < 0.8:
        return _clamp(100 - (0.8 - ratio) * 250)
    return 100


def pace_score(wpm: float | None, language: str) -> int | None:
    if wpm is None:
        return None
    low, high = sm.COMFORTABLE_WPM[sm.norm_language(language)]
    if low <= wpm <= high:
        return 100
    edge = low if wpm < low else high
    return _clamp(100 - abs(wpm - edge) / edge * 200)


def word_budget(target: int, language: str) -> int:
    """About how many words fit in the target time at a relaxed pace."""
    return int(sm.NOMINAL_WPM[sm.norm_language(language)] * target / 60)


def pitch_scores(metrics: Mapping, *, target: int, structure: Mapping | None, coverage: Sequence[Mapping]) -> dict:
    """The scoring sheet of one take, 0-100 per line. The same formula for every take, so takes compare.
    A line is None where its data is missing (no feedback yet, no key messages given, pace without a duration)."""
    language = metrics.get("language", "hu")
    pauses = metrics.get("pauses") or {}
    fluency = 100 - metrics.get("fillers_per_100_words", 0) * 8 - pauses.get("long_count", 0) * 10
    budget = word_budget(target, language)
    over_words = max(0, metrics.get("word_count", 0) - budget * 1.1)
    concision = (
        100
        - max(0.0, metrics.get("avg_sentence_words", 0) - 18) * 4
        - metrics.get("repetition", 0) * 100
        - (over_words / budget * 100 if budget else 0)
    )
    scores: dict[str, int | None] = {
        "time": time_score(metrics.get("duration_seconds"), target) if metrics.get("duration_source") != "estimated" else None,
        "pace": pace_score(metrics.get("words_per_minute"), language),
        "fluency": _clamp(fluency),
        "concision": _clamp(concision),
        "structure": (sum(25 for part in PITCH_PARTS if (structure or {}).get(part, {}).get("present"))
                      if structure else None),
        "key_messages": (_clamp(sum(1 for c in coverage if c.get("covered")) / len(coverage) * 100)
                         if coverage else None),
    }
    present = [value for value in scores.values() if value is not None]
    scores["overall"] = _clamp(sum(present) / len(present)) if present else None
    return scores


def overrun_seconds(duration: float | None, target: int) -> float:
    return round(max(0.0, (duration or 0) - target), 1)


# --- prompts and the model's answers ------------------------------------------------------------------------------

PROMPT_FILES = {
    "talk_partner": "smarttalk_partner_hu.md",
    "talk_feedback": "smarttalk_feedback_hu.md",
    "pitch_feedback": "pitch_feedback_hu.md",
    "pitch_followup": "pitch_followup_hu.md",
    "pitch_answer": "pitch_answer_feedback_hu.md",
}


def load_prompt(prompt_dir: Path | str, kind: str) -> str:
    return (Path(prompt_dir) / PROMPT_FILES[kind]).read_text(encoding="utf-8")


def render(template: str, **values: object) -> str:
    # str.replace rather than format(): the templates contain literal JSON braces.
    for key, value in values.items():
        template = template.replace("{" + key + "}", str(value))
    return template


def said(quote: str, text: str) -> bool:
    """Is ``quote`` (ignoring case and punctuation) part of ``text``?"""
    needle = " ".join(sm.words(quote))
    return bool(needle) and f" {needle} " in f" {' '.join(sm.words(text))} "


def _text(value: Any, limit: int = 600) -> str:
    return value.strip()[:limit] if isinstance(value, str) else ""


def clean_partner_reply(raw: str, limit: int = 1200) -> str:
    """The partner's line, without a speaker label or quotes the model may add."""
    reply = (raw or "").strip()
    for label in ("Partner:", "partner:", "Beszélgetőtárs:", "Partner :"):
        if reply.startswith(label):
            reply = reply[len(label):].strip()
    lines = [line for line in reply.splitlines() if line.strip()]
    # The model sometimes goes on to write the user's next line too; keep only its own.
    kept: list[str] = []
    for line in lines:
        if line.lstrip().startswith(("Felhasználó:", "User:")):
            break
        kept.append(line.strip())
    return " ".join(kept).strip().strip('"„”').strip()[:limit]


def parse_talk_feedback(data: Any, user_text: str, metrics: Mapping) -> dict:
    """Keep only well-formed, grounded remarks; fall back to a focus chosen from the metrics."""
    if not isinstance(data, Mapping):
        raise ValueError("feedback is not an object")
    observations = []
    for item in data.get("observations") or []:
        if not isinstance(item, Mapping):
            continue
        skill, note, quote = item.get("skill"), _text(item.get("note")), _text(item.get("quote"), 300)
        if skill not in TALK_SKILLS or not note:
            continue
        if quote and not said(quote, user_text):
            continue  # an invented quote: the remark is not backed by what was said
        observations.append({"skill": skill, "note": note, "quote": quote})
    strengths = [_text(item, 300) for item in (data.get("strengths") or []) if _text(item, 300)][:3]
    tip = _text(data.get("tip"))
    focus = data.get("retry_focus")
    if focus not in TALK_SKILLS:
        focus = suggest_focus(metrics)
    if not tip and not observations:
        raise ValueError("feedback has neither a tip nor observations")
    return {
        "tip": tip,
        "retry_focus": focus,
        "retry_instruction": _text(data.get("retry_instruction"), 400),
        "observations": observations[:6],
        "strengths": strengths,
    }


def parse_pitch_feedback(data: Any, transcript: str) -> dict:
    if not isinstance(data, Mapping):
        raise ValueError("feedback is not an object")
    raw = data.get("structure")
    raw_structure: Mapping = raw if isinstance(raw, Mapping) else {}
    structure = {}
    for part in PITCH_PARTS:
        found = raw_structure.get(part)
        item: Mapping = found if isinstance(found, Mapping) else {}
        quote = _text(item.get("quote"), 400)
        # Present only with a quote from the take; the note may say what is missing either way.
        present = bool(item.get("present")) and said(quote, transcript)
        structure[part] = {"present": present, "quote": quote if present else "", "note": _text(item.get("note"), 400)}
    rewordings = []
    for item in data.get("rewordings") or []:
        if not isinstance(item, Mapping):
            continue
        original, suggestion = _text(item.get("original"), 500), _text(item.get("suggestion"), 500)
        if original and suggestion and said(original, transcript) and original != suggestion:
            rewordings.append({"original": original, "suggestion": suggestion, "why": _text(item.get("why"), 300)})
    tip = _text(data.get("tip"))
    if not tip and not rewordings and not any(part["note"] for part in structure.values()):
        raise ValueError("feedback is empty")
    return {
        "structure": structure,
        "clarity": _text(data.get("clarity"), 500),
        "tip": tip,
        "rewordings": rewordings[:5],
    }


def parse_followups(data: Any) -> list[str]:
    items = data.get("questions") if isinstance(data, Mapping) else data
    questions = [_text(item, 300) for item in (items or []) if _text(item, 300)]
    if not questions:
        raise ValueError("no follow-up questions")
    return questions[:3]


def parse_answer_feedback(data: Any) -> dict:
    if not isinstance(data, Mapping):
        raise ValueError("feedback is not an object")
    note, tip = _text(data.get("note")), _text(data.get("tip"))
    if not note and not tip:
        raise ValueError("feedback is empty")
    return {"answered": bool(data.get("answered")), "note": note, "tip": tip}
