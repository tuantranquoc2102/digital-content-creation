import logging
import os
import subprocess
import uuid
from pathlib import Path
from typing import Optional

from PIL import Image, ImageColor, ImageDraw, ImageFont

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
        self.fonts_dir = Path(settings.VIDEO_FONTS_DIR)
        self.fonts_dir.mkdir(parents=True, exist_ok=True)

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

    def list_available_fonts(self) -> list[dict]:
        """Return a list of font files available in the fonts directory."""
        supported = {".ttf", ".otf", ".ttc"}
        fonts = [
            {"filename": p.name, "path": str(p)}
            for p in sorted(self.fonts_dir.iterdir())
            if p.suffix.lower() in supported
        ]
        return fonts

    def create_video_from_image_audio_ticker(
        self,
        image_path: str,
        audio_path: str,
        ticker_text: str = "",
        font_filename: str = "",
        font_size: int = 48,
        font_color: str = "white",
        stroke_color: str = "black",
        ticker_speed: float = 120,
        ticker_bottom_margin: int = 40,
    ) -> str:
        """
        Create a 1920×1080 MP4 from a static image + audio, optionally
        overlaying a right-to-left scrolling ticker text bar.

        The ticker is rendered to a transparent PNG via Pillow, then composited
        using FFmpeg's overlay filter.  This avoids the drawtext filter entirely,
        sidestepping all Windows path-escaping issues in FFmpeg filter strings.

        Args:
            image_path:           Source image (any aspect ratio).
            audio_path:           Source audio (determines video duration).
            ticker_text:          Text to scroll. Empty → no ticker overlay.
            font_filename:        Filename inside VIDEO_FONTS_DIR (e.g. "Roboto-Regular.ttf").
                                  If empty, picks the first available font.
            font_size:            Font size in pixels (default 48).
            font_color:           Colour name or hex string Pillow understands,
                                  e.g. "white", "#FFFFFF" (default "white").
            ticker_speed:         Scroll speed in pixels per second (default 120).
            ticker_bottom_margin: Distance in pixels from the bottom of the frame
                                  to the bottom edge of the ticker bar (default 40).
        Returns:
            Path to the output MP4.
        """
        output_path = self._make_output_path()
        ticker_png: Optional[str] = None
        filter_script: Optional[str] = None

        try:
            if ticker_text.strip():
                # ── Render ticker to a transparent PNG strip with Pillow ──────
                font_path = self._resolve_font(font_filename)
                ticker_png, bar_h = self._render_ticker_png(
                    text=" ".join(ticker_text.split()),
                    font_path=font_path,
                    font_size=font_size,
                    font_color=font_color,
                    stroke_color=stroke_color,
                )
                y_pos = 1080 - bar_h - ticker_bottom_margin
                s = f"{ticker_speed:.6f}"
                # x(t) = W - t*s + floor(t*s / (W+w)) * (W+w)
                # This is mod(t*s, W+w) expressed without any commas:
                #   W=1920 (background width), w=overlay width from FFmpeg 'w' var
                x_expr = f"1920-t*{s}+floor(t*{s}/(1920+w))*(1920+w)"

                # ── Write filter_complex to a script file ───────────────────
                # The filter content contains NO file paths (font/text are baked
                # into the PNG) and NO single quotes — only numeric constants and
                # safe FFmpeg expression operators.  Reading from a file bypasses
                # Python’s list2cmdline quoting entirely.
                # [bg] must be the FIRST input to overlay, [1:v] the second.
                # Using -/filter_complex <path> (replaces deprecated -filter_complex_script).
                filter_content = (
                    "[0:v]scale=1920:1080:force_original_aspect_ratio=decrease,"
                    "pad=1920:1080:(ow-iw)/2:(oh-ih)/2[bg];"
                    f"[bg][1:v]overlay=x={x_expr}:y={y_pos}[out]"
                )
                filter_script = str(
                    (self.output_dir / f"filter_{uuid.uuid4().hex[:8]}.txt").resolve()
                )
                Path(filter_script).write_bytes(filter_content.encode("utf-8"))
                logger.info(f"filter_complex: {filter_content}")

                cmd = [
                    "ffmpeg", "-y",
                    "-loop", "1", "-i", image_path,
                    "-loop", "1", "-i", ticker_png,
                    "-i", audio_path,
                    "-/filter_complex", filter_script,
                    "-map", "[out]",
                    "-map", "2:a",
                    "-c:v", "libx264",
                    "-preset", "fast",
                    "-c:a", "aac", "-b:a", "192k",
                    "-pix_fmt", "yuv420p",
                    "-shortest",
                    output_path,
                ]
            else:
                # ── No ticker: simple scale+pad via filter_script:v ───────────
                filter_content = (
                    "scale=1920:1080:force_original_aspect_ratio=decrease,"
                    "pad=1920:1080:(ow-iw)/2:(oh-ih)/2"
                )
                filter_script = str(
                    (self.output_dir / f"filter_{uuid.uuid4().hex[:8]}.txt").resolve()
                )
                Path(filter_script).write_bytes(filter_content.encode("utf-8"))

                cmd = [
                    "ffmpeg", "-y",
                    "-loop", "1", "-i", image_path,
                    "-i", audio_path,
                    "-/vf", filter_script,
                    "-c:v", "libx264",
                    "-tune", "stillimage",
                    "-preset", "fast",
                    "-c:a", "aac", "-b:a", "192k",
                    "-pix_fmt", "yuv420p",
                    "-shortest",
                    output_path,
                ]

            self._run_ffmpeg(cmd)
            return output_path

        finally:
            if ticker_png:
                Path(ticker_png).unlink(missing_ok=True)
            if filter_script:
                Path(filter_script).unlink(missing_ok=True)

    def _render_ticker_png(
        self,
        text: str,
        font_path: str,
        font_size: int,
        font_color: str,
        stroke_color: str = "black",
        box_padding: int = 12,
    ) -> tuple[str, int]:
        """
        Render ticker text to a transparent RGBA PNG using Pillow.

        Returns:
            (png_path, bar_height)
        """
        # Parse color via Pillow (supports "white", "#RRGGBB", "rgb(r,g,b)", ...)
        try:
            rgb = ImageColor.getrgb(font_color)
            text_rgba = (*rgb[:3], 255)  # type: ignore[arg-type]
        except (ValueError, AttributeError):
            text_rgba = (255, 255, 255, 255)

        try:
            s_rgb = ImageColor.getrgb(stroke_color)
            stroke_rgba = (*s_rgb[:3], 255)  # type: ignore[arg-type]
        except (ValueError, AttributeError):
            stroke_rgba = (0, 0, 0, 255)
        stroke_width = max(2, font_size // 12)

        # Load font; fall back to Pillow default if the file can’t be opened
        try:
            font = ImageFont.truetype(font_path, font_size)
        except Exception as e:
            logger.warning(f"Could not load font '{font_path}': {e} — using Pillow default")
            font = ImageFont.load_default()

        # Measure exact text bounding box on a 1-pixel scratch image
        scratch = Image.new("RGBA", (1, 1))
        draw = ImageDraw.Draw(scratch)
        bbox = draw.textbbox(
            (0, 0),
            text,
            font=font,
            stroke_width=stroke_width,
        )  # (left, top, right, bottom)
        text_w = bbox[2] - bbox[0]
        text_h = bbox[3] - bbox[1]

        bar_w = text_w + 2 * box_padding
        bar_h = text_h + 2 * box_padding

        # Render onto a transparent RGBA canvas with stroked text only.
        img = Image.new("RGBA", (bar_w, bar_h), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        draw.text(
            (box_padding - bbox[0], box_padding - bbox[1]),
            text,
            font=font,
            fill=text_rgba,
            stroke_width=stroke_width,
            stroke_fill=stroke_rgba,
        )

        png_path = str(
            (self.output_dir / f"ticker_{uuid.uuid4().hex[:10]}.png").resolve()
        )
        img.save(png_path, "PNG")
        logger.info(f"Ticker PNG rendered: {bar_w}x{bar_h}px → {png_path}")
        return png_path, bar_h

    def _resolve_font(self, font_filename: str) -> str:
        """
        Return the absolute path to a font in VIDEO_FONTS_DIR.
        If font_filename is empty, uses VIDEO_DEFAULT_FONT_FILENAME from settings.
        Raises VideoCreationError if no font can be found.
        """
        supported = {".ttf", ".otf", ".ttc"}

        if font_filename:
            candidate = self.fonts_dir / font_filename
            if not candidate.exists():
                raise VideoCreationError(
                    f"Font '{font_filename}' not found in fonts directory. "
                    f"Upload it to: {self.fonts_dir}"
                )
            return str(candidate.resolve())

        default_candidate = self.fonts_dir / settings.VIDEO_DEFAULT_FONT_FILENAME
        if default_candidate.exists() and default_candidate.suffix.lower() in supported:
            logger.info(f"Using default video font from settings: {default_candidate.name}")
            return str(default_candidate.resolve())

        # Fallback: auto-pick first available font
        for p in sorted(self.fonts_dir.iterdir()):
            if p.suffix.lower() in supported:
                logger.info(f"Auto-selected font: {p.name}")
                return str(p.resolve())

        raise VideoCreationError(
            f"No fonts found in '{self.fonts_dir}'. "
            f"Place '{settings.VIDEO_DEFAULT_FONT_FILENAME}' or another .ttf/.otf file there first."
        )

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
        # Always return an absolute path so FFmpeg never has to guess the cwd.
        return str((self.output_dir / f"video_{uuid.uuid4().hex}.mp4").resolve())

    @staticmethod
    def _run_ffmpeg(cmd: list[str]) -> None:
        logger.info(f"FFmpeg command: {' '.join(cmd)}")
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            # Show up to 2000 chars of stderr so the real first error line is visible
            raise VideoCreationError(result.stderr[-2000:])
