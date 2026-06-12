import logging
import os
import subprocess
import uuid
from pathlib import Path

from app.core.config import get_settings
from app.core.exceptions import VideoCreationError

logger = logging.getLogger(__name__)
settings = get_settings()

SUPPORTED_IMAGE_TYPES = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
SUPPORTED_VIDEO_TYPES = {".mp4", ".mov", ".avi", ".mkv", ".webm"}
SUPPORTED_AUDIO_TYPES = {".mp3", ".wav", ".m4a", ".ogg", ".flac", ".aac"}


class VideoService:
    """Service for compositing image/video with audio using FFmpeg."""

    def __init__(self):
        self.output_dir = Path(settings.VIDEO_OUTPUT_DIR)
        self.output_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def create_video_from_image_and_audio(
        self,
        image_path: str,
        audio_path: str,
    ) -> str:
        """
        Create an MP4 video from a static image + audio file.
        The video length equals the audio duration.

        Returns:
            Path to the output MP4 file.
        """
        output_path = self._make_output_path()
        cmd = [
            "ffmpeg", "-y",
            "-loop", "1",
            "-i", image_path,
            "-i", audio_path,
            # Ensure width & height are both divisible by 2 (required by libx264/yuv420p)
            "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
            "-c:v", "libx264",
            "-tune", "stillimage",
            "-c:a", "aac",
            "-b:a", "192k",
            "-pix_fmt", "yuv420p",
            "-shortest",
            output_path,
        ]
        self._run_ffmpeg(cmd)
        return output_path

    def create_video_from_video_and_audio(
        self,
        video_path: str,
        audio_path: str,
        replace_audio: bool = True,
    ) -> str:
        """
        Combine a video file with a new audio track.
        The video is looped indefinitely and trimmed to the exact audio duration,
        so the output length always equals the audio length regardless of the
        original video's duration.

        Args:
            video_path:     Source video to loop.
            audio_path:     Audio track to attach.
            replace_audio:  If True (default), discard the original video audio.

        Returns:
            Path to the output MP4 file.
        """
        output_path = self._make_output_path()
        # -stream_loop -1  → loop the video stream indefinitely
        # -shortest        → stop encoding when the audio stream ends
        cmd = [
            "ffmpeg", "-y",
            "-stream_loop", "-1",
            "-i", video_path,
            "-i", audio_path,
            "-map", "0:v:0",
            "-map", "1:a:0",
            # Re-encode video so the loop boundary is seamless and the
            # container timestamps are continuous
            "-c:v", "libx264",
            "-preset", "fast",
            "-crf", "23",
            "-pix_fmt", "yuv420p",
            "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
            "-c:a", "aac",
            "-b:a", "192k",
            "-shortest",
            output_path,
        ]
        self._run_ffmpeg(cmd)
        return output_path

    def concat_videos_with_audio(
        self,
        video_paths: list[str],
        audio_path: str,
    ) -> str:
        """
        Concatenate multiple video clips (in given order, looping if needed) to
        match the exact duration of the audio file, then replace the audio track.

        Strategy:
          1. Get audio duration via ffprobe.
          2. Keep cycling through the video list until total accumulated duration
             >= audio duration, then trim the last clip.
          3. Concat all selected clips with the concat demuxer.
          4. Replace audio track with the provided audio file.

        Returns:
            Path to the output MP4 file.
        """
        if not video_paths:
            raise VideoCreationError("No video files provided for concatenation")

        audio_duration = self._get_duration(audio_path)
        logger.info(f"Audio duration: {audio_duration:.3f}s, source clips: {len(video_paths)}")

        # Build ordered list of clip paths (cycle) until we cover audio_duration
        clip_durations = [self._get_duration(p) for p in video_paths]
        selected: list[tuple[str, float]] = []  # (path, duration_to_use)
        accumulated = 0.0
        i = 0
        while accumulated < audio_duration:
            path = video_paths[i % len(video_paths)]
            dur = clip_durations[i % len(clip_durations)]
            remaining = audio_duration - accumulated
            if dur > remaining:
                selected.append((path, remaining))
                accumulated += remaining
            else:
                selected.append((path, dur))
                accumulated += dur
            i += 1

        logger.info(f"Using {len(selected)} clip segment(s) total")

        # Write concat list
        concat_file = str((self.output_dir / f"concat_{uuid.uuid4().hex[:8]}.txt").resolve())
        concat_path = str((self.output_dir / f"concat_video_{uuid.uuid4().hex[:8]}.mp4").resolve())
        output_path = self._make_output_path()

        try:
            # Each entry: file + optional trim duration for the last segment
            with open(concat_file, "w", encoding="utf-8") as f:
                for path, dur in selected:
                    abs_path = str(Path(path).resolve()).replace("\\", "/")
                    f.write(f"file '{abs_path}'\n")
                    f.write(f"duration {dur:.6f}\n")

            # Step 1: concat clips (re-encode so timestamps are continuous)
            cmd_concat = [
                "ffmpeg", "-y",
                "-f", "concat", "-safe", "0",
                "-i", concat_file,
                "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
                "-c:v", "libx264", "-preset", "fast", "-crf", "23",
                "-pix_fmt", "yuv420p",
                "-an",
                concat_path,
            ]
            self._run_ffmpeg(cmd_concat)

            # Step 2: attach audio, trim to exact audio duration
            cmd_audio = [
                "ffmpeg", "-y",
                "-i", concat_path,
                "-i", audio_path,
                "-map", "0:v:0",
                "-map", "1:a:0",
                "-c:v", "copy",
                "-c:a", "aac", "-b:a", "192k",
                "-t", str(audio_duration),
                output_path,
            ]
            self._run_ffmpeg(cmd_audio)
        finally:
            for p in (concat_file, concat_path):
                try:
                    Path(p).unlink(missing_ok=True)
                except OSError:
                    pass

        return output_path

    def get_video_files_from_folder(self, folder_path: str) -> list[str]:
        """Return sorted list of supported video file paths from a folder."""
        folder = Path(folder_path)
        if not folder.is_dir():
            raise VideoCreationError(f"Not a directory: {folder_path}")
        files = sorted(
            str(p) for p in folder.iterdir()
            if p.suffix.lower() in SUPPORTED_VIDEO_TYPES
        )
        if not files:
            raise VideoCreationError(f"No supported video files found in: {folder_path}")
        return files

    def _get_duration(self, path: str) -> float:
        """Return media duration in seconds via ffprobe."""
        cmd = [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            path,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise VideoCreationError(f"ffprobe failed for {path}: {result.stderr[-200:]}")
        try:
            return float(result.stdout.strip())
        except ValueError:
            raise VideoCreationError(f"Could not parse duration for {path}: {result.stdout!r}")

    def cleanup(self, *paths: str) -> None:
        """Remove temporary files, silently ignoring missing ones."""
        for path in paths:
            try:
                if path and os.path.exists(path):
                    os.remove(path)
                    logger.debug(f"Cleaned up: {path}")
            except OSError as e:
                logger.warning(f"Could not remove {path}: {e}")

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _make_output_path(self) -> str:
        return str(self.output_dir / f"video_{uuid.uuid4().hex}.mp4")

    @staticmethod
    def _run_ffmpeg(cmd: list[str]) -> None:
        logger.info(f"FFmpeg command: {' '.join(cmd)}")
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise VideoCreationError(result.stderr[-500:])
