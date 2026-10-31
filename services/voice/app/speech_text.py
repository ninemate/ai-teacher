"""Turn a chat answer (often light Markdown) into plain Hungarian text that Piper reads well.

Piper phonemizes with espeak-ng, which already reads Hungarian numbers, ordinals ("1.", "XIV.") and most
abbreviations ("pl.", "stb.", "ill.", "ún.", "vö.") correctly. What it gets wrong is Markdown punctuation,
read aloud as "csillag" or "kettőskereszt", and a few abbreviations it spells letter by letter.
"""

import re
from functools import partial

# Abbreviations espeak-ng spells out ("kb." -> "ká bé"). The trailing period is kept where it may end a sentence.
ABBREVIATIONS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bkb\.(?=\s|$)", re.IGNORECASE), "körülbelül"),
    (re.compile(r"\bdb\b", re.IGNORECASE), "darab"),
    (re.compile(r"\bi\.\s?e\.(?=\s|$)", re.IGNORECASE), "időszámításunk előtt"),
    (re.compile(r"\bi\.\s?sz\.(?=\s|$)", re.IGNORECASE), "időszámításunk szerint"),
]

_CODE_FENCE = re.compile(r"^\s*(```|~~~).*$", re.MULTILINE)
_LINK = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
_URL = re.compile(r"https?://\S+")
# Source markers the RAG answer carries ("[1]", "[1, 3]", "[forrás nélkül]"); the UI shows them, speech skips them.
_CITATION = re.compile(r"\s*\[(?:\d+(?:\s*[,;]\s*\d+)*|\s*forrás\s+nélkül\s*)\]", re.IGNORECASE)
_LINE_MARKERS = re.compile(r"^\s*(?:#{1,6}\s+|>\s?|[-*+•]\s+)", re.MULTILINE)
_TABLE_RULE = re.compile(r"^\s*\|?\s*:?-{3,}.*$", re.MULTILINE)
_EMPHASIS = re.compile(r"\*+|_{2,}|`+|~~")
_SENTENCE_END = (".", "!", "?", ":", ";", ",", "…")


def _expand(replacement: str, match: re.Match[str]) -> str:
    return replacement[0].upper() + replacement[1:] if match.group(0)[:1].isupper() else replacement


def prepare_for_speech(text: str) -> str:
    """Strip Markdown, expand the abbreviations espeak-ng misreads, and end every line with punctuation."""
    text = _CODE_FENCE.sub("", text)
    text = _LINK.sub(r"\1", text)
    text = _URL.sub("", text)
    text = _CITATION.sub("", text)
    text = _TABLE_RULE.sub("", text)
    text = _LINE_MARKERS.sub("", text)
    text = _EMPHASIS.sub("", text)
    text = re.sub(r"[ \t]*\|[ \t]*", ", ", text)
    for pattern, replacement in ABBREVIATIONS:
        text = pattern.sub(partial(_expand, replacement), text)

    lines = []
    for line in text.splitlines():
        line = re.sub(r"\s+", " ", line).strip(" ,")
        if not any(char.isalnum() for char in line):
            continue
        # A heading or list item without punctuation would run into the next line with no pause.
        lines.append(line if line.endswith(_SENTENCE_END) else f"{line}.")
    return " ".join(lines)
