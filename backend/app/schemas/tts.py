from enum import IntEnum
from pydantic import BaseModel, field_validator
from typing import Optional

from app.core.config import get_settings

_settings = get_settings()


class VoiceID(IntEnum):
    """Available voices on the Zalo AI TTS page."""
    SOUTHERN_WOMAN = 1
    NORTHERN_WOMAN = 2
    SOUTHERN_MAN = 3
    NORTHERN_MAN = 4
    NORTHERN_WOMAN_2 = 5
    SOUTHERN_WOMAN_2 = 6


class TTSFromTextRequest(BaseModel):
    text: str
    chunk_size: int = _settings.TTS_CHUNK_SIZE
    voice: VoiceID = VoiceID.NORTHERN_WOMAN
    session_id: Optional[str] = None  # Provide to resume a previously interrupted job

    @field_validator("text")
    @classmethod
    def text_must_not_be_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("text must not be empty")
        return v.strip()

    @field_validator("chunk_size")
    @classmethod
    def chunk_size_range(cls, v: int) -> int:
        if not (50 <= v <= 10000):
            raise ValueError("chunk_size must be between 50 and 10000")
        return v


class TTSResponse(BaseModel):
    message: str
    chunks_processed: int
    output_filename: str
    voice: VoiceID
    session_id: str  # Use this value to resume if the job fails partway
