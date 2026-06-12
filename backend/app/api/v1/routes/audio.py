import logging
import os
from typing import Annotated

from fastapi import APIRouter, Depends, UploadFile, File, Form
from fastapi.responses import FileResponse

from app.schemas.audio import (
    YouTubeAudioRequest,
    YouTubeTranscriptionRequest,
    TranscriptionResponse,
)
from app.services.audio_service import AudioService
from app.services.transcription_service import TranscriptionService
from app.core.config import get_settings, Settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/audio", tags=["Audio"])


def get_audio_service() -> AudioService:
    return AudioService()


def get_transcription_service() -> TranscriptionService:
    return TranscriptionService()


@router.post(
    "/download",
    summary="Download audio from YouTube",
    description=(
        "Download audio from a YouTube video. "
        "Optionally trim to a specific time range using `start_time` and `end_time` (in seconds)."
    ),
    response_class=FileResponse,
)
async def download_audio_from_youtube(
    request: YouTubeAudioRequest,
    audio_svc: AudioService = Depends(get_audio_service),
    settings: Settings = Depends(get_settings),
):
    file_path = audio_svc.download_audio(
        url=request.url,
        start_time=request.start_time,
        end_time=request.end_time,
    )
    return FileResponse(
        path=file_path,
        media_type="audio/mpeg",
        filename=os.path.basename(file_path),
        background=None,
    )


@router.post(
    "/transcribe/youtube",
    response_model=TranscriptionResponse,
    summary="Transcribe audio from YouTube",
    description=(
        "Download and transcribe audio from a YouTube video. "
        "Optionally trim to a specific time range using `start_time` and `end_time` (in seconds)."
    ),
)
async def transcribe_youtube_audio(
    request: YouTubeTranscriptionRequest,
    audio_svc: AudioService = Depends(get_audio_service),
    transcription_svc: TranscriptionService = Depends(get_transcription_service),
):
    audio_path = audio_svc.download_audio(
        url=request.url,
        start_time=request.start_time,
        end_time=request.end_time,
    )
    try:
        result = transcription_svc.transcribe(
            audio_path=audio_path,
            language=request.language,
        )
    finally:
        audio_svc.cleanup(audio_path)

    return result


@router.post(
    "/transcribe/file",
    response_model=TranscriptionResponse,
    summary="Transcribe uploaded audio file",
    description="Upload an audio file and receive its transcription.",
)
async def transcribe_audio_file(
    file: Annotated[UploadFile, File(description="Audio file (mp3, wav, m4a, ogg, flac, etc.)")],
    language: Annotated[
        str | None,
        Form(description="Optional ISO-639-1 language code (e.g. 'en', 'vi'). Auto-detected if omitted."),
    ] = None,
    audio_svc: AudioService = Depends(get_audio_service),
    transcription_svc: TranscriptionService = Depends(get_transcription_service),
    settings: Settings = Depends(get_settings),
):
    from pathlib import Path
    import uuid

    output_dir = Path(settings.AUDIO_OUTPUT_DIR)
    output_dir.mkdir(parents=True, exist_ok=True)

    suffix = Path(file.filename).suffix if file.filename else ".tmp"
    temp_path = str(output_dir / f"upload_{uuid.uuid4().hex}{suffix}")

    try:
        contents = await file.read()
        with open(temp_path, "wb") as f:
            f.write(contents)

        result = transcription_svc.transcribe(
            audio_path=temp_path,
            language=language,
        )
    finally:
        audio_svc.cleanup(temp_path)

    return result
