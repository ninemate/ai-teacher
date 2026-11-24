"""Private conversation and pitch practice. Authentication and CSRF are supplied by the API middleware."""

import json
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated, Literal

import httpx
from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy.orm.exc import StaleDataError
from teacher_common import speech_metrics as metrics
from teacher_common import speech_practice as practice
from teacher_common import speech_store as store
from teacher_common.config import get_settings
from teacher_common.db import session_scope
from teacher_common.study import extract_json

Language = Literal["hu", "en"]
Audio = Annotated[UploadFile | None, File()]
Text = Annotated[str, Form(max_length=practice.MAX_PITCH_CHARS)]


class StartTalk(BaseModel):
    scenario: str
    style: str = "friendly"
    language: Language = "hu"


def owner(request: Request):
    principal = getattr(request.state, "session", None)
    if principal:
        return principal.username
    if get_settings().auth_enabled:
        raise HTTPException(401, "Bejelentkezés szükséges.")
    return "local"


def require(row):
    if row is None:
        raise HTTPException(404, "A gyakorlat nem található.")
    return row


@contextmanager
def transaction():
    try:
        with session_scope() as session:
            yield session
    except StaleDataError as exc:
        raise HTTPException(409, "A beszélgetés megváltozott. Nyisd meg újra az előzményekből.") from exc


def build_router(llm: Callable, transcribe: Callable) -> APIRouter:
    settings = get_settings()
    recordings = store.AudioStore(settings.speech_audio_dir, settings.speech_audio_keep)
    router = APIRouter(prefix="/speech", tags=["speech"])

    def generate(kind, parser=None, **values):
        template = practice.load_prompt(Path(settings.teacher_prompt_path).parent, kind)
        try:
            raw = llm(practice.render(template, **values), parser is not None)
            if parser:
                return parser(extract_json(raw))
            reply = practice.clean_partner_reply(raw)
            if not reply:
                raise ValueError("empty reply")
            return reply
        except (ValueError, TypeError, httpx.HTTPError) as exc:
            raise HTTPException(502, "A nyelvi modell válasza nem használható. Próbáld újra.") from exc

    def speech_input(text, audio, language, max_chars):
        content = stt = mime = None
        if audio is not None:
            if text.strip():
                raise HTTPException(422, "Szöveget vagy felvételt adj meg.")
            limit = int(settings.voice_max_upload_mb * 1024 * 1024)
            content = audio.file.read(limit + 1)
            if len(content) > limit:
                raise HTTPException(413, "A felvétel túl nagy.")
            mime = (audio.content_type or "").partition(";")[0].lower()
            if not content or mime not in store.AUDIO_TYPES:
                raise HTTPException(422, "Üres vagy ismeretlen hangformátum.")
            stt = transcribe(content, mime, language)
            text = stt.get("text") or ""
        text = text.strip()
        if not text or len(text) > max_chars:
            raise HTTPException(422, f'A szöveg hossza 1–{max_chars} karakter lehet.')
        return text, stt, content, mime

    def partner(scenario, style, language, turns):
        return generate("talk_partner", setting=scenario.setting, partner=scenario.partner,
                        style=practice.TALK_STYLES[style]["prompt"], language_name=practice.LANGUAGE_NAMES[language],
                        opening_rule="" if turns else "Te kezdeményezed a beszélgetést.", transcript=practice.transcript_lines(turns))

    def create(session, username, scenario, style, language, focus=None, retry_of=None):
        opening = partner(scenario, style, language, []) if scenario.partner_starts else None
        row = store.create_talk(session, username, scenario=scenario.id, style=style, language=language,
                                focus=focus, retry_of=retry_of)
        if opening:
            store.append_turns(row, {"role": "partner", "text": opening})
        return store.serialize_talk(row)

    def talk_detail(session, row):
        data = store.serialize_talk(row)
        prior = store.get_talk(session, row.username, row.retry_of) if row.retry_of else store.previous_finished_talk(session, row)
        data["comparison"] = metrics.compare(row.metrics or {}, prior.metrics if prior else None,
            ["question_turn_ratio", "longest_turn_words", "monologues", "repetition", "listening_ratio"])
        return data

    def take_detail(session, row):
        data = store.serialize_take(row)
        prior = store.previous_take(session, row)
        data["comparison"] = metrics.compare(row.scores, prior.scores if prior else None, list(row.scores))
        return data

    @router.get("/options")
    def options():
        return {"talks": [{"id": s.id, "label": s.title, "hint": s.setting, "category": practice.TALK_CATEGORIES[s.category]}
                          for s in practice.TALK_SCENARIOS],
                "takes": [{"id": s.id, "label": s.title, "hint": s.hint} for s in practice.PITCH_SCENARIOS],
                "styles": {k: v["label"] for k, v in practice.TALK_STYLES.items()},
                "interest": {k: v["label"] for k, v in practice.INTEREST_LEVELS.items()}, "skills": practice.TALK_SKILLS}

    @router.get("/history")
    def history(request: Request):
        username = owner(request)
        with session_scope() as session:
            return {"talks": [store.serialize_talk(r, full=False) for r in store.list_talks(session, username)],
                    "takes": [store.serialize_take(r, full=False) for r in store.list_takes(session, username)]}

    @router.post("/talks")
    def start(payload: StartTalk, request: Request):
        scenario = practice.talk_scenario(payload.scenario)
        if not scenario or payload.style not in practice.TALK_STYLES:
            raise HTTPException(422, "Ismeretlen helyzet vagy stílus.")
        with transaction() as session:
            return create(session, owner(request), scenario, payload.style, payload.language)

    @router.get("/talks/{talk_id}")
    def read_talk(request: Request, talk_id: int):
        with session_scope() as session:
            return talk_detail(session, require(store.get_talk(session, owner(request), talk_id)))

    @router.post("/talks/{talk_id}/turn")
    def turn(request: Request, talk_id: int, text: Text = "", audio: Audio = None):
        with transaction() as session:
            row = require(store.get_talk(session, owner(request), talk_id))
            if row.status != "active" or sum(t["role"] == "user" for t in row.turns) >= practice.MAX_TALK_TURNS:
                raise HTTPException(409, "Ehhez a beszélgetéshez nem adható több válasz.")
            text, stt, _, _ = speech_input(text, audio, row.language, practice.MAX_TURN_CHARS)
            user_turn = {"role": "user", "text": text, "metrics": metrics.analyze(text, language=row.language, stt=stt).as_dict()}
            reply = partner(practice.talk_scenario(row.scenario), row.style, row.language, [*row.turns, user_turn])
            store.append_turns(row, user_turn, {"role": "partner", "text": reply})
            return store.serialize_talk(row)

    @router.post("/talks/{talk_id}/finish")
    def finish(request: Request, talk_id: int):
        with transaction() as session:
            row = require(store.get_talk(session, owner(request), talk_id))
            if row.status == "active":
                own = "\n".join(t["text"] for t in row.turns if t["role"] == "user")
                if not own:
                    raise HTTPException(422, "Előbb szólj hozzá a beszélgetéshez.")
                summary = metrics.conversation_metrics(row.turns, language=row.language)
                feedback = generate("talk_feedback", lambda obj: practice.parse_talk_feedback(obj, own, summary),
                    setting=practice.talk_scenario(row.scenario).setting, style=practice.TALK_STYLES[row.style]["label"],
                    focus=practice.TALK_SKILLS.get(row.focus, "általános"), metrics=json.dumps(summary, ensure_ascii=False),
                    transcript=practice.transcript_lines(row.turns))
                store.finish_talk(row, summary, feedback)
            return talk_detail(session, row)

    @router.post("/talks/{talk_id}/retry")
    def retry(request: Request, talk_id: int):
        with transaction() as session:
            row = require(store.get_talk(session, owner(request), talk_id))
            if row.status != "finished":
                raise HTTPException(409, "Előbb zárd le a beszélgetést.")
            return create(session, row.username, practice.talk_scenario(row.scenario), row.style, row.language,
                          row.feedback["retry_focus"], row.id)

    @router.post("/takes")
    def take(request: Request, scenario: Annotated[str, Form()], target: Annotated[int, Form()],
             language: Annotated[Language, Form()] = "hu", interest: Annotated[str, Form()] = "medium",
             messages: Annotated[str, Form(max_length=1000)] = "", text: Text = "", audio: Audio = None):
        username = owner(request)
        situation = practice.pitch_scenario(scenario)
        if not situation or target not in practice.PITCH_TARGETS or interest not in practice.INTEREST_LEVELS:
            raise HTTPException(422, "Ismeretlen helyzet, időkeret vagy érdeklődési szint.")
        keys = [line.strip() for line in messages.splitlines() if line.strip()]
        if len(keys) > practice.MAX_KEY_MESSAGES:
            raise HTTPException(422, "Legfeljebb öt kulcsüzenetet adj meg.")
        text, stt, content, mime = speech_input(text, audio, language, practice.MAX_PITCH_CHARS)
        sheet = metrics.analyze(text, language=language, stt=stt).as_dict()
        measured = sheet["duration_source"] != "estimated"
        sheet["overrun_seconds"] = practice.overrun_seconds(sheet["duration_seconds"], target) if measured else None
        coverage = metrics.key_message_coverage(text, keys, language)
        context = dict(transcript=text, listener=situation.listener, language_name=practice.LANGUAGE_NAMES[language])
        feedback = generate("pitch_feedback", lambda obj: practice.parse_pitch_feedback(obj, text),
                            scenario=situation.title, target=target, metrics=json.dumps(sheet, ensure_ascii=False),
                            key_messages="\n".join(keys), **context)
        questions = generate("pitch_followup", practice.parse_followups,
                             interest=practice.INTEREST_LEVELS[interest]["prompt"], count=2, **context)
        scores = practice.pitch_scores(sheet, target=target, structure=feedback["structure"], coverage=coverage)
        filename = media = None
        try:
            with transaction() as session:
                if content and recordings.keep_per_user:
                    filename, media = recordings.save(username, content, mime)
                row = store.add_take(session, username, scenario=scenario, target_seconds=target, language=language,
                    input="voice" if content else "text", key_messages=keys, transcript=text, duration_seconds=sheet["duration_seconds"],
                    audio_file=filename, audio_type=media, metrics=sheet, coverage=coverage, scores=scores, feedback=feedback,
                    followups={"interest": interest, "questions": [{"question": q} for q in questions]})
                expired = store.prune_audio(session, username, recordings)
                data = take_detail(session, row)
        except Exception:
            recordings.delete(username, filename)
            raise
        for name in expired:
            recordings.delete(username, name)
        return data

    @router.get("/takes/{take_id}")
    def read_take(request: Request, take_id: int):
        with session_scope() as session:
            return take_detail(session, require(store.get_take(session, owner(request), take_id)))

    @router.post("/takes/{take_id}/answer")
    def answer(request: Request, take_id: int, index: Annotated[int, Form(ge=0)],
               text: Annotated[str, Form(max_length=practice.MAX_TURN_CHARS)] = "", audio: Audio = None):
        with transaction() as session:
            row = require(store.get_take(session, owner(request), take_id))
            followups = dict(row.followups or {})
            questions = [dict(q) for q in followups.get("questions") or []]
            if index >= len(questions):
                raise HTTPException(404, "A kérdés nem található.")
            text, stt, _, _ = speech_input(text, audio, row.language, practice.MAX_TURN_CHARS)
            sheet = metrics.analyze(text, language=row.language, stt=stt).as_dict()
            feedback = generate("pitch_answer", practice.parse_answer_feedback,
                                listener=practice.pitch_scenario(row.scenario).listener,
                                question=questions[index]["question"], answer=text,
                                metrics=json.dumps(sheet, ensure_ascii=False))
            questions[index].update(answer=text, metrics=sheet, feedback=feedback)
            row.followups = {**followups, "questions": questions}
            return take_detail(session, row)

    @router.get("/takes/{take_id}/audio")
    def audio_file(request: Request, take_id: int):
        with session_scope() as session:
            row = require(store.get_take(session, owner(request), take_id))
            path = require(recordings.path(row.username, row.audio_file))
            return FileResponse(path, media_type=row.audio_type, headers={"Cache-Control": "private, no-store"})

    return router
