"""Média: safe image loading, preprocessing, local OCR and the per-user /media API (with a fake model and OCR)."""

import io
import json
import shutil
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw, ImageFont
from teacher_common import media


def _png(image: Image.Image) -> bytes:
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


def text_image(text="Hello world\nGood morning", size=(900, 300), font_size=56) -> Image.Image:
    image = Image.new("RGB", size, "white")
    font = ImageFont.load_default(size=font_size)
    ImageDraw.Draw(image).multiline_text((40, 40), text, fill="black", font=font, spacing=20)
    return image


# --- image safety ---------------------------------------------------------------------------------------------------

def test_load_image_rejects_non_images_and_unsupported_formats():
    with pytest.raises(media.ImageRejected) as err:
        media.load_image(b"not an image at all")
    assert err.value.status == 415
    gif = io.BytesIO()
    Image.new("RGB", (20, 20)).save(gif, format="GIF")
    with pytest.raises(media.ImageRejected):
        media.load_image(gif.getvalue())


def test_load_image_rejects_oversized_files_and_decompression_bombs():
    with pytest.raises(media.ImageRejected) as err:
        media.load_image(b"\0" * (media.MAX_BYTES + 1))
    assert err.value.status == 413
    bomb = _png(Image.new("1", (6000, 5000)))  # 30 megapixels in a few kB
    assert len(bomb) < 100_000
    with pytest.raises(media.ImageRejected) as err:
        media.load_image(bomb)
    assert err.value.status == 413


def test_exif_rotation_is_applied_and_metadata_dropped():
    image = Image.new("RGB", (400, 200), "white")
    exif = Image.Exif()
    exif[0x0112] = 6  # Orientation: rotate 90° clockwise to view
    exif[0x010F] = "Camera maker"
    raw = io.BytesIO()
    image.save(raw, format="JPEG", exif=exif.tobytes())
    upright = media.load_image(raw.getvalue())
    assert upright.size == (200, 400)
    kept = Image.open(io.BytesIO(media.preview_jpeg(upright)))
    assert kept.format == "JPEG" and not kept.getexif()


def test_preprocess_straightens_a_tilted_page():
    page = Image.new("L", (1200, 900), 255)
    draw = ImageDraw.Draw(page)
    for y in range(120, 800, 60):  # lines of "text"
        draw.rectangle((100, y, 1100, y + 22), fill=0)
    tilted = page.rotate(5, expand=True, fillcolor=255)
    assert abs(media._skew_angle(tilted) + 5) <= 0.6
    _, info = media.preprocess(tilted.convert("RGB"))
    assert abs(info["deskew_degrees"] + 5) <= 0.6
    _, straight = media.preprocess(page.convert("RGB"))
    assert straight["deskew_degrees"] == 0 or abs(straight["deskew_degrees"]) < 0.5


def test_guess_direction():
    assert media.guess_direction("Kérjük, a jegyét őrizze meg az utazás végéig.") == "hu-en"
    assert media.guess_direction("Please keep the ticket until the end of the journey.") == "en-hu"


@pytest.mark.skipif(shutil.which("tesseract") is None, reason="tesseract is not installed")
def test_tesseract_reads_a_generated_image_with_confidences():
    prepared, _ = media.preprocess(text_image())
    result = media.tesseract_ocr(prepared, "eng")
    assert "Hello" in result.text and "morning" in result.text
    assert len([line for line in result.lines if line]) == 2
    assert result.mean_confidence and result.mean_confidence > 50


# --- API ------------------------------------------------------------------------------------------------------------

class FakeLLM:
    def __init__(self):
        self.prompts = []
        self.fail = False

    def __call__(self, prompt, json_mode):
        if self.fail:
            raise RuntimeError("model down")
        self.prompts.append((prompt, json_mode))
        if json_mode:
            return json.dumps({"pairs": [{"front": "jó reggelt", "back": "good morning"}]})
        if "Magyarázd" in prompt:
            return "- jó reggelt: köszönés"
        return "Good morning, world"


def fake_ocr(image):
    return media.OcrResult(lines=[
        [media.OcrWord("Jó", 95.0), media.OcrWord("reggelt", 91.0)],
        [],
        [media.OcrWord("világ", 42.0)],
    ])


def make_client(llm=None, **kwargs):
    """The router behind a stand-in for the session middleware: the x-user header says who is signed in."""
    from media_routes import build_router
    from teacher_common.db import init_db

    init_db()
    llm = llm or FakeLLM()
    app = FastAPI()

    @app.middleware("http")
    async def set_test_user(request, call_next):
        request.state.session = SimpleNamespace(username=request.headers.get("x-user", "media-a"), role="user")
        return await call_next(request)

    app.include_router(build_router(llm=llm, ocr=fake_ocr, **kwargs))
    client = TestClient(app)
    client.llm = llm
    return client


@pytest.fixture
def api():
    return make_client()


def upload(client, user="media-a", data=None, direction="auto"):
    data = data if data is not None else _png(text_image())
    return client.post("/media/items", files={"image": ("photo.png", data, "image/png")},
                       data={"direction": direction}, headers={"x-user": user})


def test_photo_to_text_translation_explanation_and_deck(api):
    from teacher_common import study_store
    from teacher_common.db import session_scope

    resp = upload(api)
    assert resp.status_code == 201, resp.text
    item = resp.json()
    assert item["text"] == "Jó reggelt\n\nvilág"
    assert item["direction"] == "hu-en" and item["translation"] is None
    assert item["lines"][2] == [{"text": "világ", "confidence": 42.0}]
    assert item["low_confidence"] == 70.0 and item["mean_confidence"] == 76.0

    image = api.get(f"/media/items/{item['id']}/image")
    assert image.status_code == 200 and image.headers["content-type"] == "image/jpeg"

    translated = api.post(f"/media/items/{item['id']}/translate", json={"direction": "hu-en"}).json()
    assert translated["translation"] == "Good morning, world"
    prompt, _ = api.llm.prompts[-1]
    assert "magyar szöveget angolra" in prompt and "Jó reggelt" in prompt

    explained = api.post(f"/media/items/{item['id']}/explain").json()
    assert explained["explanation"].startswith("- jó reggelt")
    assert api.get(f"/media/items/{item['id']}").json()["explanation"] == explained["explanation"]

    # Nothing goes to the study material until the user saves a deck.
    with session_scope() as session:
        decks_before = len(study_store.list_decks(session, workspace_ids={"user:media-a"}))
    glossary = api.post(f"/media/items/{item['id']}/glossary", json={"phrases": ["jó reggelt", "világ"]}).json()
    assert glossary["pairs"] == [{"front": "jó reggelt", "back": "good morning"}, {"front": "világ", "back": ""}]
    with session_scope() as session:
        assert len(study_store.list_decks(session, workspace_ids={"user:media-a"})) == decks_before

    saved = api.post(f"/media/items/{item['id']}/deck",
                     json={"pairs": [{"front": "jó reggelt", "back": "good morning"}, {"front": "világ", "back": "world"}]})
    assert saved.status_code == 201 and saved.json()["card_count"] == 2
    with session_scope() as session:
        deck = study_store.get_deck(session, saved.json()["deck_id"], workspace_ids={"user:media-a"})
    assert [c["front"] for c in deck["cards"]] == ["jó reggelt", "világ"]
    assert deck["title"] == "Fotó: Jó reggelt"

    assert api.delete(f"/media/items/{item['id']}").status_code == 204
    assert api.get(f"/media/items/{item['id']}").status_code == 404
    assert all(row["id"] != item["id"] for row in api.get("/media/items").json())


def test_photos_are_private_to_their_owner(api):
    item = upload(api, user="media-a").json()
    other = {"x-user": "media-b"}
    assert all(row["id"] != item["id"] for row in api.get("/media/items", headers=other).json())
    base = f"/media/items/{item['id']}"
    assert api.get(base, headers=other).status_code == 404
    assert api.get(f"{base}/image", headers=other).status_code == 404
    assert api.post(f"{base}/translate", json={"direction": "hu-en"}, headers=other).status_code == 404
    assert api.post(f"{base}/explain", headers=other).status_code == 404
    assert api.post(f"{base}/glossary", json={"phrases": ["x"]}, headers=other).status_code == 404
    assert api.post(f"{base}/deck", json={"pairs": [{"front": "a", "back": "b"}]}, headers=other).status_code == 404
    assert api.delete(base, headers=other).status_code == 404
    assert api.get(base).status_code == 200  # still there for its owner
    api.delete(base)


def test_bad_uploads_are_refused(api):
    assert upload(api, data=b"<svg xmlns='http://www.w3.org/2000/svg'/>").status_code == 415
    assert upload(api, data=_png(Image.new("1", (6000, 5000)))).status_code == 413
    assert upload(api, direction="de-fr").status_code == 422


def test_model_failure_keeps_the_recognised_text(api):
    item = upload(api).json()
    api.llm.fail = True
    resp = api.post(f"/media/items/{item['id']}/translate", json={"direction": "hu-en"})
    assert resp.status_code == 502
    assert api.get(f"/media/items/{item['id']}").json()["text"] == item["text"]
    api.delete(f"/media/items/{item['id']}")


def test_vision_model_is_used_when_configured_and_tesseract_is_the_fallback():
    calls = []

    def vision(jpeg):
        calls.append(jpeg[:2])
        return "Please keep your ticket"

    item = upload(make_client(vision=vision), user="media-v").json()
    assert calls == [b"\xff\xd8"]  # it got the metadata-free JPEG copy
    assert item["engine"] == "vision" and item["direction"] == "en-hu" and item["mean_confidence"] is None

    def broken(jpeg):
        raise RuntimeError("no such model")

    fallback = upload(make_client(vision=broken), user="media-v").json()
    assert fallback["engine"] == "tesseract" and fallback["text"].startswith("Jó reggelt")
