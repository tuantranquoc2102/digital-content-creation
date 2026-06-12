import logging
import os
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, UploadFile
from fastapi.responses import FileResponse

from app.core.config import Settings, get_settings
from app.schemas.tts import TTSFromTextRequest, TTSResponse, VoiceID
from app.services.tts_service import TTSService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/tts", tags=["Text to Speech"])


def get_tts_service() -> TTSService:
    return TTSService()


@router.post(
    "/from-text",
    response_model=TTSResponse,
    summary="Convert plain text to speech",
    description=(
        "Accepts a text string, splits it into chunks, drives the "
        "[Zalo AI TTS page](https://ai.zalo.solutions/products/text-to-audio-converter) "
        "via a headless browser for each chunk, and returns a single stitched MP3. "
        "Use `chunk_size` to control how many characters are sent per request (default 10000)."
    ),
)
async def tts_from_text(
    request: TTSFromTextRequest,
    tts_svc: TTSService = Depends(get_tts_service),
):
    output_path, chunks_count, session_id = await tts_svc.text_to_speech(
        text=request.text,
        chunk_size=request.chunk_size,
        voice_id=request.voice.value,
        session_id=request.session_id,
    )
    return TTSResponse(
        message="Text successfully converted to speech",
        chunks_processed=chunks_count,
        output_filename=os.path.basename(output_path),
        voice=request.voice,
        session_id=session_id,
    )


@router.post(
    "/from-text/download",
    summary="Convert plain text to speech and download MP3",
    description=(
        "Same as `/from-text` but streams the resulting MP3 directly as a file download."
    ),
    response_class=FileResponse,
)
async def tts_from_text_download(
    request: TTSFromTextRequest,
    tts_svc: TTSService = Depends(get_tts_service),
):
    output_path, _, _sid = await tts_svc.text_to_speech(
        text=request.text,
        chunk_size=request.chunk_size,
        voice_id=request.voice.value,
        session_id=request.session_id,
    )
    return FileResponse(
        path=output_path,
        media_type="audio/mpeg",
        filename=os.path.basename(output_path),
    )


@router.post(
    "/from-file/download",
    summary="Convert text file to speech and download MP3",
    description=(
        "Upload a plain-text file (`.txt`). "
        "The file is read, split into chunks and synthesised via the Zalo AI TTS page. "
        "Returns the stitched MP3 as a file download."
    ),
    response_class=FileResponse,
)
async def tts_from_file_download(
    file: Annotated[
        UploadFile,
        File(description="Plain-text file (.txt) to convert to speech"),
    ],
    chunk_size: Annotated[
        int,
        Form(description="Max characters per TTS request chunk (50–10000). Defaults to TTS_CHUNK_SIZE in .env."),
    ] = 0,  # 0 = use TTS_CHUNK_SIZE from settings
    voice: Annotated[
        VoiceID,
        Form(description="Voice: 1=Southern woman, 2=Northern woman, 3=Southern man, 4=Northern man, 5=Northern woman 2, 6=Southern woman 2"),
    ] = VoiceID.NORTHERN_WOMAN,
    tts_svc: TTSService = Depends(get_tts_service),
    settings: Settings = Depends(get_settings),
):
    raw = await file.read()
    try:
        text = raw.decode("utf-8-sig")  # utf-8-sig strips BOM if present
    except UnicodeDecodeError:
        text = raw.decode("latin-1")

    output_path, _, _sid = await tts_svc.text_to_speech(
        text=text,
        chunk_size=chunk_size or None,
        voice_id=voice.value,
    )
    return FileResponse(
        path=output_path,
        media_type="audio/mpeg",
        filename=os.path.basename(output_path),
    )
