import json
import os
import uuid
import subprocess
import logging
import zipfile
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
        start_time: Optional[float] = None,
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

            effective_start_time = 0.0 if start_time is None else start_time
            needs_trim = start_time is not None or end_time is not None
            if needs_trim:
                trimmed_path = str(self.output_dir / f"{file_id}_trimmed.mp3")
                self._trim_audio(raw_mp3, trimmed_path, effective_start_time, end_time)
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

    # ------------------------------------------------------------------
    # Multi-segment trim
    # ------------------------------------------------------------------

    def trim_audio_to_segments(
        self,
        input_path: str,
        segments: list[dict],
    ) -> str:
        """
        Trim *input_path* into multiple segments and return the path to a ZIP
        archive containing one MP3 per segment.

        Each segment dict must have ``start_time`` (float, seconds) and
        ``end_time`` (float, seconds).  An optional ``label`` key is used as
        the filename stem inside the ZIP.
        """
        if not segments:
            raise AudioDownloadError("At least one segment is required")

        session_id = uuid.uuid4().hex
        segment_paths: list[tuple[str, str]] = []  # (file_path, arcname)

        for idx, seg in enumerate(segments, start=1):
            start = float(seg["start_time"])
            end = float(seg["end_time"])
            if end <= start:
                raise AudioDownloadError(
                    f"Segment {idx}: end_time ({end}) must be greater than start_time ({start})"
                )
            label = str(seg.get("label") or f"segment_{idx:03d}")
            out_path = str(self.output_dir / f"{session_id}_{idx:03d}.mp3")
            self._trim_audio(input_path, out_path, start, end)
            arcname = f"{label}.mp3"
            segment_paths.append((out_path, arcname))

        zip_path = str(self.output_dir / f"{session_id}_segments.zip")
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for file_path, arcname in segment_paths:
                zf.write(file_path, arcname=arcname)

        for file_path, _ in segment_paths:
            try:
                os.remove(file_path)
            except OSError:
                pass

        return zip_path

    # ------------------------------------------------------------------
    # Join multiple audio files
    # ------------------------------------------------------------------

    def join_audio_files(self, input_paths: list[str]) -> str:
        """
        Concatenate *input_paths* in order and return the path to a single
        merged MP3 file.  Uses FFmpeg concat demuxer for a fast, lossless join.
        """
        if not input_paths:
            raise AudioDownloadError("At least one audio file is required for joining")

        session_id = uuid.uuid4().hex
        concat_list_path = str(self.output_dir / f"{session_id}_concat.txt")
        output_path = str(self.output_dir / f"{session_id}_joined.mp3")

        try:
            with open(concat_list_path, "w", encoding="utf-8") as f:
                for path in input_paths:
                    abs_path = str(Path(path).resolve(strict=False)).replace("\\", "/")
                    safe = abs_path.replace("'", "'\\''")
                    f.write(f"file '{safe}'\n")

            cmd = [
                "ffmpeg", "-y",
                "-f", "concat",
                "-safe", "0",
                "-i", concat_list_path,
                "-c", "copy",
                output_path,
            ]
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode != 0:
                raise AudioDownloadError(f"FFmpeg join error: {result.stderr}")

            return str(Path(output_path).resolve(strict=False))

        finally:
            try:
                os.remove(concat_list_path)
            except OSError:
                pass
