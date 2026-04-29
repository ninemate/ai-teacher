from fastapi import FastAPI, File, HTTPException, UploadFile
from pydantic import BaseModel


app = FastAPI(title="teacher-agent-voice", version="0.1.0")


class SpeakRequest(BaseModel):
    text: str


@app.get("/health")
def health():
    return {
        "status": "ok",
        "note": "Voice service scaffold only. Enable STT/TTS backends in a later phase.",
    }


@app.post("/voice/transcribe")
def transcribe(audio: UploadFile = File(...)):
    raise HTTPException(
        status_code=501,
        detail=(
            f"Voice transcription is not enabled in the MVP scaffold. "
            f"Received file: {audio.filename or 'audio'}."
        ),
    )


@app.post("/voice/speak")
def speak(payload: SpeakRequest):
    raise HTTPException(
        status_code=501,
        detail="Voice synthesis is not enabled in the MVP scaffold.",
    )
