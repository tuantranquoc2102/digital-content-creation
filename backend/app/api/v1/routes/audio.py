import json
import logging
import os
import re
from typing import Annotated

from fastapi import APIRouter, Depends, Request, UploadFile, File, Form, HTTPException, status
from fastapi.responses import FileResponse
from starlette.datastructures import UploadFile as StarletteUploadFile

from app.schemas.audio import (
    AudioSegment,
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


@router.post(
    "/trim",
    summary="Trim an uploaded audio file into multiple segments",
    description=(
        "Upload an audio file and provide a JSON array of time segments. "
        "Each segment requires `start_time` and `end_time` (seconds, `HH:MM:SS`, or `MM:SS`) "
        "and an optional `label` used as the filename inside the returned ZIP. "
        "Returns a **ZIP** archive containing one MP3 per segment.\n\n"
        "Example `segments` value:\n"
        "```json\n"
        '[\n  {"start_time": 0, "end_time": 30, "label": "intro"},\n'
        '  {"start_time": 60, "end_time": 120, "label": "verse1"}\n]\n'
        "```"
    ),
    response_class=FileResponse,
)
async def trim_audio_segments(
    file: Annotated[
        UploadFile,
        File(description="Audio file to trim (mp3, wav, m4a, ogg, flac, etc.)"),
    ],
    segments: Annotated[
        str,
        Form(
            description=(
                "JSON array of segment objects. "
                "Each object must have `start_time` and `end_time`, "
                "and an optional `label` for the output filename."
            )
        ),
    ],
    audio_svc: AudioService = Depends(get_audio_service),
    settings: Settings = Depends(get_settings),
):
    from pathlib import Path
    import uuid

    try:
        raw_segments = json.loads(segments)
        if not isinstance(raw_segments, list) or len(raw_segments) == 0:
            raise ValueError("segments must be a non-empty JSON array")
        validated = [AudioSegment.model_validate(s).model_dump() for s in raw_segments]
    except (json.JSONDecodeError, ValueError) as exc:
        from fastapi import HTTPException, status
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))

    output_dir = Path(settings.AUDIO_OUTPUT_DIR)
    output_dir.mkdir(parents=True, exist_ok=True)

    suffix = Path(file.filename).suffix if file.filename else ".mp3"
    temp_path = str(output_dir / f"upload_{uuid.uuid4().hex}{suffix}")

    try:
        contents = await file.read()
        with open(temp_path, "wb") as f:
            f.write(contents)

        zip_path = audio_svc.trim_audio_to_segments(temp_path, validated)
    finally:
        audio_svc.cleanup(temp_path)

    return FileResponse(
        path=zip_path,
        media_type="application/zip",
        filename=os.path.basename(zip_path),
    )


@router.post(
    "/join",
    summary="Join multiple audio files into one",
    description=(
        "Upload two or more audio files. "
        "They are concatenated **in the order received** and returned as a single MP3. "
        "All files must have compatible encoding (same sample rate & channel layout) "
        "for a lossless copy-stream join. "
        "If files differ in encoding, re-encode them to MP3 first before joining."
    ),
    response_class=FileResponse,
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "multipart/form-data": {
                    "schema": {
                        "type": "object",
                        "required": ["files"],
                        "properties": {
                            "files": {
                                "type": "array",
                                "items": {"type": "string", "format": "binary"},
                                "description": "Two or more audio files to join, in the desired order.",
                            },
                        },
                    }
                }
            },
        }
    },
)
async def join_audio_files(
    request: Request,
    audio_svc: AudioService = Depends(get_audio_service),
    settings: Settings = Depends(get_settings),
):
    from pathlib import Path
    import uuid

    def _parse_paths(value: str) -> list[str]:
        raw = value.strip()
        quoted = re.findall(r'"([^"]+)"|\'([^\']+)\'', raw)
        if quoted:
            return [a or b for a, b in quoted if (a or b).strip()]
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return [str(x).strip() for x in parsed if str(x).strip()]
            if isinstance(parsed, str):
                return [parsed.strip()]
        except json.JSONDecodeError:
            pass
        sep = "\n" if "\n" in raw else ","
        return [p.strip() for p in raw.split(sep) if p.strip()]

    output_dir = Path(settings.AUDIO_OUTPUT_DIR)
    output_dir.mkdir(parents=True, exist_ok=True)

    input_paths: list[str] = []
    temp_paths: list[str] = []
    try:
        form = await request.form()
        for item in form.getlist("files"):
            if isinstance(item, (UploadFile, StarletteUploadFile)):
                suffix = Path(item.filename).suffix if item.filename else ".mp3"
                temp_path = str(output_dir / f"upload_{uuid.uuid4().hex}{suffix}")
                contents = await item.read()
                with open(temp_path, "wb") as f:
                    f.write(contents)
                temp_paths.append(temp_path)
                input_paths.append(temp_path)
            elif isinstance(item, str) and item.strip():
                for p in _parse_paths(item):
                    resolved = Path(p).expanduser()
                    if not resolved.exists() or not resolved.is_file():
                        raise HTTPException(
                            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail=f"File not found: {p}",
                        )
                    input_paths.append(str(resolved))

        if len(input_paths) < 2:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="At least 2 audio files are required for joining",
            )

        joined_path = audio_svc.join_audio_files(input_paths)
    finally:
        for p in temp_paths:
            audio_svc.cleanup(p)

    return FileResponse(
        path=joined_path,
        media_type="audio/mpeg",
        filename=os.path.basename(joined_path),
    )
