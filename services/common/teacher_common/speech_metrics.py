"""Measurable speech metrics from a transcript, for any speaking feature (Smart Talk, Elevator Speech, tutors).

Everything here is computed from what was said and, where the speech-to-text gives them, its timestamps:
pace, pauses, filler words (Hungarian and English), duration, question ratio, sentence length, repetition and
key-message coverage. Nothing is inferred about the speaker as a person. Pure functions, no I/O, no model calls.

Timestamps are optional: ``segments`` (or words) with ``start``/``end`` seconds give real pauses and speaking
time; with only a total ``duration`` the pace is still exact but pauses are unknown; with text alone the duration
is estimated from a typical speaking pace and marked as such.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field

LANGUAGES = ("hu", "en")

# Single-word fillers and hesitation sounds. Kept conservative: words that are just as often meaningful
# ("tehát", "like", "so") are left out, so a count here is a count of hesitations, not of style.
FILLER_WORDS: dict[str, frozenset[str]] = {
    "hu": frozenset({
        "ö", "öö", "ööö", "őő", "őőő", "ő", "hm", "hmm", "mm", "öhm", "ömm", "izé", "szóval", "hát",
        "úgymond", "gyakorlatilag", "lényegében", "igazából", "ugye", "mondjuk", "na",
    }),
    "en": frozenset({
        "um", "umm", "uh", "uhh", "er", "erm", "ah", "hmm", "mm", "basically", "literally", "actually",
    }),
}
FILLER_PHRASES: dict[str, tuple[str, ...]] = {
    "hu": ("hogy is mondjam", "na most", "meg minden", "meg ilyenek", "vagy valami", "ilyen izé"),
    "en": ("you know", "i mean", "sort of", "kind of", "or something", "and stuff", "you see"),
}

# Words that open an open question (one that cannot be answered with yes or no).
OPEN_QUESTION_STARTS: dict[str, frozenset[str]] = {
    "hu": frozenset({
        "mi", "mit", "mik", "miért", "mire", "miről", "miben", "mivel", "minek", "hogyan", "milyen", "mikor",
        "hol", "honnan", "hova", "hová", "ki", "kit", "kivel", "kinek", "mennyi", "mennyire", "melyik", "meséld",
        "mesélj", "meséljen", "mondd", "mondja", "szerinted", "szerinte", "szerintetek",
    }),
    "en": frozenset({
        "what", "why", "how", "which", "where", "when", "who", "whom", "whose", "tell", "describe", "explain",
    }),
}

# Short function words skipped when counting repetition, coverage and topic overlap.
STOPWORDS: dict[str, frozenset[str]] = {
    "hu": frozenset(
        "a az egy és is de hogy nem van volt lesz meg már még csak mint ha mert vagy ez ezt azt itt ott én te ő mi"
        " ti ők engem téged neki nekem neked nagyon igen sem se pedig akkor most amit ami aki ahol amikor mikor"
        " lett vagyok vagyunk vannak volna lehet kell majd olyan ilyen így úgy minden mind sok kicsit"
        # greetings and goodbyes: returning one is not picking up the partner's topic
        " szia sziasztok helló hello szervusz üdv jó napot reggelt estét viszlát viszontlátásra köszi köszönöm".split()
    ),
    "en": frozenset(
        "a an the and or but if so of to in on at for with by from as is are was were be been being am i you he"
        " she it we they me him her us them my your our their this that these those there here not no yes do does"
        " did have has had will would can could should just very really also then than too about"
        " hi hello hey bye goodbye thanks thank morning evening".split()
    ),
}

# Typical relaxed speaking pace (words per minute) and the band that still sounds natural. Hungarian words are
# longer (agglutination), so the same speech gives fewer words per minute than in English.
NOMINAL_WPM = {"hu": 120.0, "en": 145.0}
COMFORTABLE_WPM = {"hu": (95.0, 150.0), "en": (115.0, 170.0)}

PAUSE_SECONDS = 0.6  # a gap at least this long between timed segments counts as a pause
LONG_PAUSE_SECONDS = 2.0
MONOLOGUE_WORDS = 70  # a conversational turn longer than this is a monologue
LONG_SENTENCE_WORDS = 25

_WORD = re.compile(r"[^\W\d_]+(?:[-'’][^\W\d_]+)*", re.UNICODE)
_SENTENCE_END = re.compile(r"(?<=[.!?…])\s+|\n+")


@dataclass(frozen=True)
class Segment:
    start: float
    end: float
    text: str = ""


def norm_language(language: str | None) -> str:
    return language if language in LANGUAGES else "hu"


def words(text: str) -> list[str]:
    """Lower-cased words, digits and punctuation dropped."""
    return [match.group(0).lower().replace("’", "'") for match in _WORD.finditer(text or "")]


def sentences(text: str) -> list[str]:
    parts = [part.strip() for part in _SENTENCE_END.split((text or "").strip())]
    return [part for part in parts if words(part)]


def content_words(text: str, language: str) -> list[str]:
    stop = STOPWORDS[norm_language(language)]
    return [word for word in words(text) if len(word) > 2 and word not in stop]


def stem(word: str) -> str:
    """A crude stem for matching inflected forms (Hungarian suffixes): the first five letters."""
    return word[:5]


def is_question(sentence: str) -> bool:
    return sentence.rstrip().endswith("?")


def is_open_question(sentence: str, language: str) -> bool:
    """A question opened by a question word. Hungarian often puts a particle or a name first ("És mit
    gondolsz?", "Anna, hogyan..."), so the first three words are checked."""
    if not is_question(sentence):
        return False
    starts = OPEN_QUESTION_STARTS[norm_language(language)]
    return any(word in starts for word in words(sentence)[:3])


def count_fillers(text: str, language: str) -> Counter[str]:
    language = norm_language(language)
    tokens = words(text)
    found: Counter[str] = Counter(token for token in tokens if token in FILLER_WORDS[language])
    joined = " " + " ".join(tokens) + " "
    for phrase in FILLER_PHRASES[language]:
        hits = joined.count(f" {phrase} ")
        if hits:
            found[phrase] += hits
    return found


def parse_segments(raw: Iterable[Mapping] | None) -> list[Segment]:
    """Timed segments from a speech-to-text answer, sorted; unusable entries are skipped."""
    result: list[Segment] = []
    for item in raw or []:
        try:
            start, end = float(item["start"]), float(item["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if end >= start >= 0:
            result.append(Segment(start, end, str(item.get("text") or item.get("word") or "")))
    return sorted(result, key=lambda segment: segment.start)


def timed_units(stt: Mapping | None) -> list[Segment]:
    """The finest timing a speech-to-text answer offers: its words if every segment has them, else its segments."""
    if not stt:
        return []
    raw_segments = [s for s in (stt.get("segments") or []) if isinstance(s, Mapping)]
    if raw_segments and all(s.get("words") for s in raw_segments):
        return parse_segments([w for s in raw_segments for w in s["words"]])
    if stt.get("words"):
        return parse_segments(stt["words"])
    return parse_segments(raw_segments)


@dataclass
class Pauses:
    count: int = 0
    long_count: int = 0
    total_seconds: float = 0.0
    longest_seconds: float = 0.0


def find_pauses(
    units: Sequence[Segment], *, threshold: float = PAUSE_SECONDS, long: float = LONG_PAUSE_SECONDS
) -> Pauses:
    pauses = Pauses()
    for previous, current in zip(units, units[1:], strict=False):
        gap = current.start - previous.end
        if gap >= threshold:
            pauses.count += 1
            pauses.total_seconds += gap
            pauses.longest_seconds = max(pauses.longest_seconds, gap)
            if gap >= long:
                pauses.long_count += 1
    pauses.total_seconds = round(pauses.total_seconds, 2)
    pauses.longest_seconds = round(pauses.longest_seconds, 2)
    return pauses


def repetition_ratio(text: str, language: str) -> float:
    """Share of content-word three-grams that occur more than once: 0 none repeated, 1 everything repeated."""
    tokens = [stem(word) for word in content_words(text, language)]
    grams = list(zip(tokens, tokens[1:], tokens[2:], strict=False))
    if len(grams) < 2:
        return 0.0
    counts = Counter(grams)
    repeated = sum(count for count in counts.values() if count > 1)
    return round(repeated / len(grams), 3)


def repeated_words(text: str, language: str, *, min_count: int = 3, limit: int = 5) -> list[dict]:
    """Content words (by stem) said at least ``min_count`` times, most frequent first."""
    by_stem: dict[str, Counter[str]] = {}
    for word in content_words(text, language):
        if len(word) > 3:
            by_stem.setdefault(stem(word), Counter())[word] += 1
    rows = [(sum(forms.values()), forms.most_common(1)[0][0]) for forms in by_stem.values()]
    rows = [row for row in rows if row[0] >= min_count]
    rows.sort(key=lambda row: (-row[0], row[1]))
    return [{"word": word, "count": count} for count, word in rows[:limit]]


def key_message_coverage(text: str, key_messages: Sequence[str], language: str) -> list[dict]:
    """For each key message: was it said? Matches by stems of its content words (at least half of them)."""
    said = {stem(word) for word in content_words(text, language)}
    result = []
    for message in key_messages:
        stems = {stem(word) for word in content_words(message, language)} or {stem(w) for w in words(message)}
        hit = len(stems & said)
        needed = max(1, (len(stems) + 1) // 2)
        result.append({"message": message, "covered": bool(stems) and hit >= needed, "matched": hit, "of": len(stems)})
    return result


@dataclass
class SpeechMetrics:
    language: str
    word_count: int
    sentence_count: int
    duration_seconds: float | None
    duration_source: str  # "timestamps" | "audio" | "given" | "estimated"
    speaking_seconds: float | None
    words_per_minute: float | None
    pace: str | None  # "slow" | "ok" | "fast"; None when the duration is only estimated
    pauses: Pauses | None  # None without timestamps
    filler_count: int
    fillers: dict[str, int]
    fillers_per_100_words: float
    question_count: int
    open_question_count: int
    question_ratio: float  # questions / sentences
    avg_sentence_words: float
    longest_sentence_words: int
    long_sentences: int
    repetition: float
    lexical_variety: float  # distinct / all words
    repeated_words: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


def analyze(
    text: str,
    *,
    language: str = "hu",
    duration: float | None = None,
    stt: Mapping | None = None,
    duration_is_audio: bool = True,
) -> SpeechMetrics:
    """Metrics of one stretch of speech.

    ``stt`` is the speech-to-text answer (``segments``/``words`` with seconds are used when present, else its
    ``duration``); ``duration`` overrides the length (a recording's or one the user timed, then pass
    ``duration_is_audio=False``). Without either, the duration is estimated from the nominal pace,
    ``duration_source`` says ``estimated`` and pace is left out, since it would only echo the assumption.
    """
    language = norm_language(language)
    tokens = words(text)
    parts = sentences(text)
    units = timed_units(stt)
    pauses: Pauses | None = None
    speaking: float | None = None
    if duration is None and stt and stt.get("duration") is not None:
        try:
            duration = float(stt["duration"])
        except (TypeError, ValueError):
            duration = None
    if units:
        pauses = find_pauses(units)
        speaking = round(units[-1].end - units[0].start, 2)
        total = duration if duration and duration >= speaking else speaking
        source = "timestamps"
    elif duration:
        total = float(duration)
        source = "audio" if duration_is_audio else "given"
    else:
        total = round(len(tokens) / NOMINAL_WPM[language] * 60, 1) if tokens else 0.0
        source = "estimated"
    basis = speaking or total
    wpm = round(len(tokens) / basis * 60, 1) if basis and tokens and source != "estimated" else None
    pace = None
    if wpm is not None:
        low, high = COMFORTABLE_WPM[language]
        pace = "slow" if wpm < low else "fast" if wpm > high else "ok"
    fillers = count_fillers(text, language)
    filler_total = sum(fillers.values())
    lengths = [len(words(part)) for part in parts] or [0]
    questions = [part for part in parts if is_question(part)]
    return SpeechMetrics(
        language=language,
        word_count=len(tokens),
        sentence_count=len(parts),
        duration_seconds=round(total, 2) if total else None,
        duration_source=source,
        speaking_seconds=speaking,
        words_per_minute=wpm,
        pace=pace,
        pauses=pauses,
        filler_count=filler_total,
        fillers=dict(fillers.most_common()),
        fillers_per_100_words=round(filler_total / len(tokens) * 100, 1) if tokens else 0.0,
        question_count=len(questions),
        open_question_count=sum(1 for part in questions if is_open_question(part, language)),
        question_ratio=round(len(questions) / len(parts), 3) if parts else 0.0,
        avg_sentence_words=round(sum(lengths) / len(lengths), 1),
        longest_sentence_words=max(lengths),
        long_sentences=sum(1 for length in lengths if length > LONG_SENTENCE_WORDS),
        repetition=repetition_ratio(text, language),
        lexical_variety=round(len(set(tokens)) / len(tokens), 3) if tokens else 0.0,
        repeated_words=repeated_words(text, language),
    )


# --- conversations ----------------------------------------------------------------------------------------------


def conversation_metrics(
    turns: Sequence[Mapping], *, language: str = "hu", speaker: str = "user", topic_words: Iterable[str] = ()
) -> dict:
    """Structure of a conversation from the ``speaker``'s side. ``turns`` are ``{"role", "text"}`` in order.

    - question ratio: the speaker's turns that contain a question; open questions among their questions
    - monologues: turns longer than ``MONOLOGUE_WORDS`` words; talk share: the speaker's words of all words
    - active listening: replies that pick up a content word of the partner's previous turn
    - repetition: content three-grams the speaker repeated across the conversation
    - staying on topic: of the speaker's replies that carry content words, the share that pick up the partner's
      previous turn or one of ``topic_words`` (the situation's words); greetings and goodbyes do not count
    """
    language = norm_language(language)
    own = [turn for turn in turns if turn.get("role") == speaker]
    own_text = "\n".join(str(turn.get("text") or "") for turn in own)
    own_words = [len(words(str(turn.get("text") or ""))) for turn in own]
    total_own = sum(own_words)
    all_words = sum(len(words(str(turn.get("text") or ""))) for turn in turns)
    questions = open_questions = with_question = 0
    for turn in own:
        asked = [part for part in sentences(str(turn.get("text") or "")) if is_question(part)]
        questions += len(asked)
        open_questions += sum(1 for part in asked if is_open_question(part, language))
        with_question += bool(asked)
    references = replies = on_topic = topical_replies = 0
    scenario = {stem(word) for word in content_words(" ".join(topic_words), language)}
    previous_other: str | None = None
    for turn in turns:
        if turn.get("role") != speaker:
            previous_other = str(turn.get("text") or "")
            continue
        if previous_other is not None:
            replies += 1
            theirs = {stem(word) for word in content_words(previous_other, language)}
            mine = {stem(word) for word in content_words(str(turn.get("text") or ""), language)}
            references += bool(theirs & mine)
            if mine:
                topical_replies += 1
                on_topic += bool((theirs | scenario) & mine)
            previous_other = None
    filler_total = sum(count_fillers(own_text, language).values())
    return {
        "turns": len(own),
        "words": total_own,
        "avg_turn_words": round(total_own / len(own_words), 1) if own_words else 0.0,
        "longest_turn_words": max(own_words, default=0),
        "monologues": sum(1 for count in own_words if count > MONOLOGUE_WORDS),
        "talk_share": round(total_own / all_words, 3) if all_words else 0.0,
        "questions": questions,
        "open_questions": open_questions,
        "question_turn_ratio": round(with_question / len(own), 3) if own else 0.0,
        "open_question_ratio": round(open_questions / questions, 3) if questions else 0.0,
        "listening_replies": replies,
        "listening_references": references,
        "listening_ratio": round(references / replies, 3) if replies else 0.0,
        "on_topic_replies": topical_replies,
        "on_topic_ratio": round(on_topic / topical_replies, 3) if topical_replies else None,
        "repetition": repetition_ratio(own_text, language),
        "repeated_words": repeated_words(own_text, language),
        "fillers": filler_total,
        "fillers_per_100_words": round(filler_total / total_own * 100, 1) if total_own else 0.0,
    }


def topic_overlap(text: str, topic_words: Iterable[str], language: str) -> float:
    """Share of the given topic words (by stem) that the text uses."""
    wanted = {stem(word.lower()) for word in topic_words if word}
    if not wanted:
        return 0.0
    said = {stem(word) for word in content_words(text, language)}
    return round(len(wanted & said) / len(wanted), 3)


def compare(current: Mapping, previous: Mapping | None, keys: Sequence[str]) -> dict[str, dict]:
    """Change of the numeric ``keys`` (dotted paths allowed) from an earlier attempt to this one."""
    if not previous:
        return {}

    def pick(data: Mapping, path: str):
        value: object = data
        for part in path.split("."):
            if not isinstance(value, Mapping):
                return None
            value = value.get(part)
        return value if isinstance(value, int | float) and not isinstance(value, bool) else None

    result = {}
    for key in keys:
        now, before = pick(current, key), pick(previous, key)
        if now is not None and before is not None:
            result[key] = {"now": now, "before": before, "change": round(now - before, 3)}
    return result
