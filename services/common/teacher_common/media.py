"""Photo text recognition: safe image loading, preprocessing and local OCR.

Everything runs on this machine: Pillow for decoding and cleanup, Tesseract for the text. A photo is checked
before it is decoded (format and pixel count from the header), so a small file that unpacks to a huge bitmap
(a decompression bomb) is refused without allocating it.
"""

import io
import re
import warnings
from dataclasses import dataclass, field

from PIL import Image, ImageOps, UnidentifiedImageError

ALLOWED_FORMATS = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}
MAX_BYTES = 10 * 1024 * 1024
MAX_PIXELS = 24_000_000  # a 6000×4000 phone photo
# Tesseract reads best around 300 dpi: shrink huge photos, enlarge small screenshots.
OCR_MAX_SIDE = 3500
OCR_MIN_SIDE = 1000
PREVIEW_MAX_SIDE = 1600
MAX_DESKEW_DEGREES = 10.0


class ImageRejected(ValueError):
    """The upload is not an image we process safely. ``status`` is the HTTP status to answer with."""

    def __init__(self, message: str, status: int = 415):
        super().__init__(message)
        self.status = status


@dataclass
class OcrWord:
    text: str
    confidence: float | None  # 0-100; None when the engine gives none (vision model)


@dataclass
class OcrResult:
    lines: list[list[OcrWord]] = field(default_factory=list)  # an empty list is a paragraph break
    engine: str = "tesseract"

    @property
    def text(self) -> str:
        return "\n".join(" ".join(w.text for w in line) for line in self.lines).strip()

    @property
    def mean_confidence(self) -> float | None:
        scored = [w.confidence for line in self.lines for w in line if w.confidence is not None]
        return round(sum(scored) / len(scored), 1) if scored else None

    def to_json(self) -> list[list[dict]]:
        return [[{"text": w.text, "confidence": w.confidence} for w in line] for line in self.lines]


def load_image(data: bytes) -> Image.Image:
    """Decode an upload safely: size, real format and pixel count are checked before the bitmap is read."""
    if len(data) > MAX_BYTES:
        raise ImageRejected(f"A kép legfeljebb {MAX_BYTES // (1024 * 1024)} MB lehet.", 413)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            image = Image.open(io.BytesIO(data), formats=list(ALLOWED_FORMATS))
            width, height = image.size
            if width * height > MAX_PIXELS:
                raise ImageRejected("A kép felbontása túl nagy (legfeljebb 24 megapixel).", 413)
            image.load()
    except ImageRejected:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise ImageRejected("A kép felbontása túl nagy (legfeljebb 24 megapixel).", 413) from exc
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError) as exc:
        raise ImageRejected("JPG, PNG vagy WebP képet tölts fel; ez a fájl nem olvasható.") from exc
    # Turn it the way the phone held it; transposing also drops the EXIF block, location included.
    return ImageOps.exif_transpose(image).convert("RGB")


def preview_jpeg(image: Image.Image) -> bytes:
    """The copy we keep: upright, at most PREVIEW_MAX_SIDE pixels, re-encoded without any metadata."""
    copy = image.copy()
    copy.thumbnail((PREVIEW_MAX_SIDE, PREVIEW_MAX_SIDE))
    out = io.BytesIO()
    copy.save(out, format="JPEG", quality=85)
    return out.getvalue()


def _skew_angle(gray: Image.Image) -> float:
    """Estimate the text's tilt by rotating a small ink mask and keeping the angle with the sharpest rows."""
    import numpy as np

    small = gray.copy()
    small.thumbnail((800, 800))
    pixels = np.asarray(small, dtype=np.float32)
    threshold = pixels.mean() - pixels.std() * 0.5
    ink = Image.fromarray(((pixels < threshold) * 255).astype(np.uint8))
    if np.count_nonzero(np.asarray(ink)) < 200:
        return 0.0

    def score(angle: float) -> float:
        rows = np.asarray(ink.rotate(angle, resample=Image.Resampling.NEAREST, expand=True, fillcolor=0), dtype=np.float32).sum(axis=1)
        return float(np.sum(np.diff(rows) ** 2))

    coarse = max((a / 2 for a in range(int(-MAX_DESKEW_DEGREES * 2), int(MAX_DESKEW_DEGREES * 2) + 1)), key=score)
    fine = max((coarse + a / 10 for a in range(-5, 6)), key=score)
    return round(fine, 1)


def preprocess(image: Image.Image) -> tuple[Image.Image, dict]:
    """Grayscale, scale to a readable size, stretch contrast and straighten a tilted photo."""
    gray = image.convert("L")
    longest = max(gray.size)
    if longest > OCR_MAX_SIDE:
        gray.thumbnail((OCR_MAX_SIDE, OCR_MAX_SIDE), Image.Resampling.LANCZOS)
    elif longest < OCR_MIN_SIDE:
        factor = OCR_MIN_SIDE / longest
        gray = gray.resize((round(gray.width * factor), round(gray.height * factor)), Image.Resampling.LANCZOS)
    gray = ImageOps.autocontrast(gray, cutoff=1)
    try:
        angle = _skew_angle(gray)
    except ImportError:  # numpy missing: skip straightening, OCR still works
        angle = 0.0
    if abs(angle) >= 0.5:
        gray = gray.rotate(angle, resample=Image.Resampling.BICUBIC, expand=True, fillcolor=255)
    return gray, {"deskew_degrees": angle, "size": list(gray.size)}


def tesseract_ocr(image: Image.Image, language: str, timeout: int = 60) -> OcrResult:
    """Words with confidences, grouped into lines; a new block or paragraph leaves an empty line."""
    import pytesseract

    data = pytesseract.image_to_data(image, lang=language, output_type=pytesseract.Output.DICT, timeout=timeout)
    result = OcrResult()
    current_key = None
    current_par = None
    for i, raw in enumerate(data["text"]):
        word = (raw or "").strip()
        conf = float(data["conf"][i])
        if not word or conf < 0:
            continue
        par = (data["block_num"][i], data["par_num"][i])
        key = (*par, data["line_num"][i])
        if key != current_key:
            if current_par is not None and par != current_par:
                result.lines.append([])
            result.lines.append([])
            current_key, current_par = key, par
        result.lines[-1].append(OcrWord(word, round(conf, 1)))
    return result


def result_from_text(text: str, engine: str) -> OcrResult:
    """Plain text (e.g. from a vision model) as an OcrResult without confidences."""
    lines = [[OcrWord(w, None) for w in line.split()] for line in text.strip().splitlines()]
    return OcrResult(lines=lines, engine=engine)


_HU_LETTERS = set("áéíóöőúüűÁÉÍÓÖŐÚÜŰ")
_HU_WORDS = {"a", "az", "és", "hogy", "nem", "egy", "is", "van", "meg", "már", "csak", "ez", "de", "mint", "vagy"}
_EN_WORDS = {"the", "and", "of", "to", "is", "in", "that", "it", "for", "you", "your", "with", "on", "this", "are", "be",
             "please", "not", "no", "or", "we", "will", "can", "at", "by", "from"}


def guess_direction(text: str) -> str:
    """'hu-en' for Hungarian text, 'en-hu' for English, by accented letters and common short words.
    Text with no Hungarian sign at all (no accents, no Hungarian function words) counts as English."""
    words = re.findall(r"\w+", text.lower())
    hu = sum(w in _HU_WORDS for w in words) + 3 * sum(c in _HU_LETTERS for c in text)
    en = 3 * sum(w in _EN_WORDS for w in words)
    return "en-hu" if en > hu or (hu == 0 and words) else "hu-en"
