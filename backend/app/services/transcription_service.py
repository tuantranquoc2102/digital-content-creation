import logging
from typing import Optional

import whisper

from app.core.config import get_settings
from app.core.exceptions import TranscriptionError
from app.schemas.audio import TranscriptionResponse

logger = logging.getLogger(__name__)
settings = get_settings()


class TranscriptionService:
    """Service for transcribing audio using OpenAI Whisper."""

    _model = None

    def _get_model(self):
        """Lazy-load Whisper model (singleton per process)."""
        if TranscriptionService._model is None:
            logger.info(
                f"Loading Whisper model '{settings.WHISPER_MODEL}' on device '{settings.WHISPER_DEVICE}'"
            )
            TranscriptionService._model = whisper.load_model(
                settings.WHISPER_MODEL,
                device=settings.WHISPER_DEVICE,
            )
        return TranscriptionService._model

    def transcribe(
        self,
        audio_path: str,
        language: Optional[str] = None,
    ) -> TranscriptionResponse:
        """
        Transcribe an audio file.

        Args:
            audio_path: Path to the audio file.
            language:   Optional ISO-639-1 language code (e.g. "en", "vi").
                        If None, Whisper auto-detects.

        Returns:
            TranscriptionResponse with text, language, segments, and duration.
        """
        try:
            model = self._get_model()
            decode_options = {}
            if language:
                decode_options["language"] = language

            logger.info(f"Transcribing audio: {audio_path}")
            result = model.transcribe(audio_path, **decode_options)

            segments = [
                {
                    "id": seg["id"],
                    "start": seg["start"],
                    "end": seg["end"],
                    "text": seg["text"].strip(),
                }
                for seg in result.get("segments", [])
            ]

            duration = segments[-1]["end"] if segments else None

            return TranscriptionResponse(
                text=result["text"].strip(),
                language=result.get("language"),
                segments=segments,
                duration=duration,
            )

        except Exception as e:
            logger.error(f"Transcription error: {e}")
            raise TranscriptionError(str(e))
