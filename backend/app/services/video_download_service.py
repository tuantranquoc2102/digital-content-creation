import logging
import subprocess
import uuid
from pathlib import Path

import yt_dlp

from app.core.config import get_settings
from app.core.exceptions import VideoDownloadError

logger = logging.getLogger(__name__)
settings = get_settings()


class VideoDownloadService:
    """Service for downloading source videos via yt-dlp."""

    def __init__(self):
        self.output_dir = Path(settings.VIDEO_OUTPUT_DIR)
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def download_from_youtube(self, url: str) -> str:
        return self._download_video(url, platform="youtube")

    def download_from_facebook(self, url: str) -> str:
        return self._download_video(url, platform="facebook")

    def download_from_douyin(self, url: str) -> str:
        return self._download_video(url, platform="douyin")

    @staticmethod
    def _is_hevc_codec(codec_name: str) -> bool:
        if not codec_name:
            return False
        codec = codec_name.lower()
        return codec in {"hevc", "h265", "hev1", "hvc1"} or "hevc" in codec or "h265" in codec

    def _build_ydl_options(self, output_template: str, platform: str) -> dict:
        ydl_opts = {
            "outtmpl": output_template,
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
        }

        if platform == "douyin":
            ydl_opts["format"] = "bestvideo+bestaudio/best"
            ydl_opts["merge_output_format"] = "mp4"
        else:
            ydl_opts["format"] = "best[ext=mp4]/best"

        return ydl_opts

    def _probe_video_codec(self, video_path: str) -> str:
        cmd = [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=codec_name",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            video_path,
        ]
        try:
            proc = subprocess.run(cmd, check=True, capture_output=True, text=True)
            return (proc.stdout or "").strip().lower()
        except Exception as e:
            logger.warning(f"Could not probe video codec for {video_path}: {e}")
            return ""

    def _transcode_to_h264_mp4(self, input_path: str, platform: str, file_id: str) -> str:
        output_path = str(self.output_dir / f"{platform}_{file_id}_h264.mp4")
        cmd = [
            "ffmpeg",
            "-y",
            "-i",
            input_path,
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "23",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-movflags",
            "+faststart",
            output_path,
        ]
        try:
            subprocess.run(cmd, check=True, capture_output=True, text=True)
            return str(Path(output_path).resolve())
        except subprocess.CalledProcessError as e:
            stderr = (e.stderr or "").strip()
            raise VideoDownloadError(
                f"Failed to convert Douyin video to H.264 MP4 for compatibility: {stderr or e}"
            )

    def _ensure_douyin_playback_compatibility(self, output_path: str, file_id: str) -> str:
        source_path = Path(output_path)
        codec = self._probe_video_codec(str(source_path))
        needs_conversion = source_path.suffix.lower() != ".mp4" or self._is_hevc_codec(codec) or not codec

        if not needs_conversion:
            return str(source_path.resolve())

        converted_path = self._transcode_to_h264_mp4(str(source_path), platform="douyin", file_id=file_id)
        source_path.unlink(missing_ok=True)
        return converted_path

    def _download_video(self, url: str, platform: str) -> str:
        file_id = uuid.uuid4().hex
        output_template = str(self.output_dir / f"{platform}_{file_id}.%(ext)s")
        ydl_opts = self._build_ydl_options(output_template, platform)

        try:
            logger.info(f"Downloading {platform} video from: {url}")
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)

            requested_downloads = info.get("requested_downloads") or []
            if requested_downloads:
                file_path = requested_downloads[0].get("filepath")
                if file_path and Path(file_path).exists():
                    resolved = str(Path(file_path).resolve())
                    if platform == "douyin":
                        return self._ensure_douyin_playback_compatibility(resolved, file_id)
                    return resolved

            candidate_paths = sorted(self.output_dir.glob(f"{platform}_{file_id}.*"))
            if candidate_paths:
                resolved = str(candidate_paths[0].resolve())
                if platform == "douyin":
                    return self._ensure_douyin_playback_compatibility(resolved, file_id)
                return resolved

            raise VideoDownloadError("Could not locate downloaded output file")

        except yt_dlp.utils.DownloadError as e:
            raise VideoDownloadError(str(e))
        except VideoDownloadError:
            raise
        except Exception as e:
            logger.error(f"Unexpected error during {platform} video download: {e}")
            raise VideoDownloadError(str(e))