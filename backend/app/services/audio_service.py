import os
import uuid
import subprocess
import logging
from pathlib import Path
from typing import Optional

import yt_dlp

from app.core.config import get_settings
from app.core.exceptions import AudioDownloadError
from app.core.utils import seconds_to_ffmpeg_time

logger = logging.getLogger(__name__)
settings = get_settings()


class AudioService:
    """Service for downloading and processing audio from YouTube."""

    def __init__(self):
        self.output_dir = Path(settings.AUDIO_OUTPUT_DIR)
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def _build_ydl_opts(self, output_path: str) -> dict:
        return {
            "format": "bestaudio/best",
            "outtmpl": output_path,
            "postprocessors": [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "192",
                }
            ],
            "quiet": True,
            "no_warnings": True,
        }

    def _trim_audio(
        self,
        input_path: str,
        output_path: str,
        start_time: float,
        end_time: Optional[float],
    ) -> None:
        """Trim audio file using FFmpeg."""
        cmd = ["ffmpeg", "-y", "-i", input_path, "-ss", seconds_to_ffmpeg_time(start_time)]
        if end_time is not None:
            duration = end_time - start_time
            cmd += ["-t", str(duration)]
        cmd += ["-acodec", "copy", output_path]

        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise AudioDownloadError(f"FFmpeg trimming error: {result.stderr}")

    def download_audio(
        self,
        url: str,
        start_time: float = 0.0,
        end_time: Optional[float] = None,
    ) -> str:
        """
        Download audio from YouTube URL with optional time range trimming.

        Returns:
            Path to the downloaded (and optionally trimmed) audio file.
        """
        file_id = uuid.uuid4().hex
        raw_path = str(self.output_dir / f"{file_id}_raw")
        raw_mp3 = f"{raw_path}.mp3"

        try:
            logger.info(f"Downloading audio from: {url}")
            ydl_opts = self._build_ydl_opts(raw_path)
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])

            needs_trim = start_time > 0 or end_time is not None
            if needs_trim:
                trimmed_path = str(self.output_dir / f"{file_id}_trimmed.mp3")
                self._trim_audio(raw_mp3, trimmed_path, start_time, end_time)
                os.remove(raw_mp3)
                return trimmed_path

            return raw_mp3

        except yt_dlp.utils.DownloadError as e:
            raise AudioDownloadError(str(e))
        except Exception as e:
            logger.error(f"Unexpected error during audio download: {e}")
            raise AudioDownloadError(str(e))

    def cleanup(self, file_path: str) -> None:
        """Remove a temporary audio file."""
        try:
            if os.path.exists(file_path):
                os.remove(file_path)
                logger.debug(f"Cleaned up temp file: {file_path}")
        except OSError as e:
            logger.warning(f"Failed to remove temp file {file_path}: {e}")
