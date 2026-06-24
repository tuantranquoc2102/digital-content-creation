import json
import logging
import mimetypes
import os
import uuid
from pathlib import Path
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse

from app.core.config import Settings, get_settings
from app.schemas.video import (
    DouyinVideoDownloadRequest,
    FacebookVideoDownloadRequest,
    FacebookProfileDownloadRequest,
    YouTubeVideoDownloadRequest,
)
from app.services.video_service import (
    VideoService,
    SUPPORTED_AUDIO_TYPES,
    SUPPORTED_IMAGE_TYPES,
    SUPPORTED_VIDEO_TYPES,
)
from app.services.video_download_service import VideoDownloadService
from app.core.exceptions import VideoCreationError, UnsupportedFileTypeError

logger = logging.getLogger(__name__)
default_settings = get_settings()

router = APIRouter(prefix="/video", tags=["Video"])


def get_video_service() -> VideoService:
    return VideoService()


def get_video_download_service() -> VideoDownloadService:
    return VideoDownloadService()


async def _save_upload(file: UploadFile, output_dir: Path, allowed_types: set[str]) -> str:
    """Persist an uploaded file to disk and return its path."""
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in allowed_types:
        raise UnsupportedFileTypeError(suffix, allowed_types)
    dest = str(output_dir / f"upload_{uuid.uuid4().hex}{suffix}")
    contents = await file.read()
    with open(dest, "wb") as f:
        f.write(contents)
    return dest


@router.get(
    "/fonts",
    summary="List available ticker fonts",
    description="Returns the list of font files available on the server for ticker text rendering.",
)
async def list_fonts(
    video_svc: VideoService = Depends(get_video_service),
):
    return {"fonts": video_svc.list_available_fonts()}


@router.post(
    "/download/youtube",
    summary="Download video from YouTube",
    description="Download a single YouTube video and return it as a file.",
    response_class=FileResponse,
)
async def download_youtube_video(
    request: YouTubeVideoDownloadRequest,
    download_svc: VideoDownloadService = Depends(get_video_download_service),
):
    output_path = download_svc.download_from_youtube(request.url)
    media_type = mimetypes.guess_type(output_path)[0] or "application/octet-stream"
    return FileResponse(
        path=output_path,
        media_type=media_type,
        filename=os.path.basename(output_path),
    )


@router.post(
    "/download/facebook",
    summary="Download video from Facebook",
    description="Download a single Facebook video and return it as a file.",
    response_class=FileResponse,
)
async def download_facebook_video(
    request: FacebookVideoDownloadRequest,
    download_svc: VideoDownloadService = Depends(get_video_download_service),
):
    output_path = download_svc.download_from_facebook(request.url)
    media_type = mimetypes.guess_type(output_path)[0] or "application/octet-stream"
    return FileResponse(
        path=output_path,
        media_type=media_type,
        filename=os.path.basename(output_path),
    )


@router.post(
    "/download/douyin",
    summary="Download video from Douyin",
    description="Download a single Douyin video and return it as a file.",
    response_class=FileResponse,
)
async def download_douyin_video(
    request: DouyinVideoDownloadRequest,
    download_svc: VideoDownloadService = Depends(get_video_download_service),
):
    output_path = download_svc.download_from_douyin(request.url)
    media_type = mimetypes.guess_type(output_path)[0] or "application/octet-stream"
    return FileResponse(
        path=output_path,
        media_type=media_type,
        filename=os.path.basename(output_path),
    )


@router.post(
    "/download/facebook/profile",
    summary="Download all videos from a Facebook profile / Reels page",
    description=(
        "Scrape and download up to `max_videos` videos from a Facebook profile, "
        "page, or Reels tab URL (e.g. `https://www.facebook.com/people/…/?sk=reels_tab`). "
        "Returns a **ZIP** archive containing all downloaded MP4 files. "
        "Public pages work without authentication. "
        "Private or friend-only content requires the server to have valid Facebook cookies "
        "configured via `FACEBOOK_COOKIES_FILE` in `.env`."
    ),
    response_class=FileResponse,
)
async def download_facebook_profile_videos(
    request: FacebookProfileDownloadRequest,
    download_svc: VideoDownloadService = Depends(get_video_download_service),
):
    zip_path = download_svc.download_all_from_facebook_profile(
        request.url, request.max_videos
    )
    return FileResponse(
        path=zip_path,
        media_type="application/zip",
        filename=os.path.basename(zip_path),
    )


@router.post(
    "/from-image",
    summary="Create 1920×1080 video from image + audio with optional scrolling ticker",
    description=(
        "Upload a **static image** and an **audio file**. "
        "Returns a **1920×1080 MP4** where the image fills the frame for the full audio duration. "
        "Optionally overlay a right-to-left scrolling ticker text bar. "
        "Use `GET /fonts` to see available font files."
    ),
    response_class=FileResponse,
)
async def create_video_from_image(
    image: Annotated[
        UploadFile,
        File(description=f"Image file. Supported: {', '.join(SUPPORTED_IMAGE_TYPES)}"),
    ],
    audio: Annotated[
        UploadFile,
        File(description=f"Audio file. Supported: {', '.join(SUPPORTED_AUDIO_TYPES)}"),
    ],
    ticker_text: Annotated[
        str,
        Form(description="Text to scroll right-to-left across the video. Leave empty for no ticker."),
    ] = "",
    font_filename: Annotated[
        str,
        Form(description="Font filename from the server fonts directory. "
                         "Defaults to VIDEO_DEFAULT_FONT_FILENAME from .env when omitted."),
    ] = default_settings.VIDEO_DEFAULT_FONT_FILENAME,
    font_size: Annotated[
        int,
        Form(description="Ticker font size in pixels (default 48)."),
    ] = 48,
    font_color: Annotated[
        str,
        Form(description="Ticker text colour, e.g. 'white' or '#FFFFFF' (default 'white')."),
    ] = "white",
    stroke_color: Annotated[
        str,
        Form(description="Ticker text outline/border colour, e.g. 'black' or '#000000' (default 'black')."),
    ] = "black",
    ticker_speed: Annotated[
        float,
        Form(description="Ticker scroll speed in pixels per second (default 120)."),
    ] = 120,
    ticker_bottom_margin: Annotated[
        int,
        Form(description="Distance in pixels from the bottom of the video to the ticker text bottom edge (default 40)."),
    ] = 40,
    video_svc: VideoService = Depends(get_video_service),
    settings: Settings = Depends(get_settings),
):
    temp_dir = Path(settings.VIDEO_OUTPUT_DIR)
    temp_dir.mkdir(parents=True, exist_ok=True)

    image_path = audio_path = None
    try:
        image_path = await _save_upload(image, temp_dir, SUPPORTED_IMAGE_TYPES)
        audio_path = await _save_upload(audio, temp_dir, SUPPORTED_AUDIO_TYPES)

        output_path = video_svc.create_video_from_image_audio_ticker(
            image_path=image_path,
            audio_path=audio_path,
            ticker_text=ticker_text,
            font_filename=font_filename,
            font_size=font_size,
            font_color=font_color,
            stroke_color=stroke_color,
            ticker_speed=ticker_speed,
            ticker_bottom_margin=ticker_bottom_margin,
        )
        return FileResponse(
            path=output_path,
            media_type="video/mp4",
            filename=os.path.basename(output_path),
        )
    finally:
        video_svc.cleanup(image_path, audio_path)


@router.post(
    "/from-video",
    summary="Replace audio in an existing video",
    description=(
        "Upload a **video file** and an **audio file**. "
        "Returns an MP4 video with the original audio replaced by the provided audio track. "
        "Output length is capped to the shorter of the two streams."
    ),
    response_class=FileResponse,
)
async def create_video_with_audio(
    video: Annotated[
        UploadFile,
        File(description=f"Video file. Supported: {', '.join(SUPPORTED_VIDEO_TYPES)}"),
    ],
    audio: Annotated[
        UploadFile,
        File(description=f"Audio file. Supported: {', '.join(SUPPORTED_AUDIO_TYPES)}"),
    ],
    replace_audio: Annotated[
        bool,
        Form(description="Strip original audio and replace with the uploaded audio track (default: true)."),
    ] = True,
    video_svc: VideoService = Depends(get_video_service),
    settings: Settings = Depends(get_settings),
):
    temp_dir = Path(settings.VIDEO_OUTPUT_DIR)
    temp_dir.mkdir(parents=True, exist_ok=True)

    video_path = audio_path = None
    try:
        video_path = await _save_upload(video, temp_dir, SUPPORTED_VIDEO_TYPES)
        audio_path = await _save_upload(audio, temp_dir, SUPPORTED_AUDIO_TYPES)

        output_path = video_svc.create_video_from_video_and_audio(
            video_path, audio_path, replace_audio=replace_audio
        )
        return FileResponse(
            path=output_path,
            media_type="video/mp4",
            filename=os.path.basename(output_path),
        )
    finally:
        video_svc.cleanup(video_path, audio_path)


@router.get(
    "/browse",
    summary="Browse server-side folders/files",
    description=(
        "List the contents of a server-side directory. "
        "Returns folders and supported video files separately. "
        "Pass `path` to navigate into a subfolder; omit it to start from the filesystem roots."
    ),
)
async def browse_filesystem(
    path: Optional[str] = None,
):
    import os as _os

    if path is None:
        # Return drive roots on Windows, / on Unix
        import string
        drives = [f"{d}:\\" for d in string.ascii_uppercase if _os.path.exists(f"{d}:\\")]
        return {
            "current": None,
            "parent": None,
            "folders": [{"name": d, "path": d} for d in drives],
            "video_files": [],
        }

    folder = Path(path)
    if not folder.is_dir():
        raise HTTPException(status_code=400, detail=f"Not a directory: {path}")

    try:
        entries = sorted(folder.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
    except PermissionError:
        raise HTTPException(status_code=403, detail=f"Permission denied: {path}")

    folders = [
        {"name": e.name, "path": str(e)}
        for e in entries if e.is_dir() and not e.name.startswith(".")
    ]
    video_files = [
        {"name": e.name, "path": str(e)}
        for e in entries if e.is_file() and e.suffix.lower() in SUPPORTED_VIDEO_TYPES
    ]

    parent = str(folder.parent) if folder.parent != folder else None
    return {
        "current": str(folder),
        "parent": parent,
        "folders": folders,
        "video_files": video_files,
    }


@router.post(
    "/concat-with-audio",
    summary="Concat multiple videos + audio → single MP4",
    description=(
        "Upload an **audio file** and one of:\n"
        "- `videos`: multiple video files uploaded directly\n"
        "- `video_folder`: server-side absolute folder path (videos sorted by filename)\n"
        "- `video_paths`: JSON array of server-side absolute file paths, e.g. "
        '`["C:/videos/a.mp4","C:/videos/b.mp4"]`\n\n'
        "All video clips are concatenated in order, looping if necessary, and trimmed to "
        "exactly match the audio duration. The audio track replaces any original video audio.\n\n"
        "Supported video types: `" + ", ".join(sorted(SUPPORTED_VIDEO_TYPES)) + "`\n\n"
        "Supported audio types: `" + ", ".join(sorted(SUPPORTED_AUDIO_TYPES)) + "`"
    ),
    response_class=FileResponse,
)
async def concat_videos_with_audio(
    request: Request,
    audio: Annotated[
        UploadFile,
        File(description=f"Audio file. Supported: {', '.join(SUPPORTED_AUDIO_TYPES)}"),
    ],
    video_folder: Annotated[
        Optional[str],
        Form(description="Server-side absolute folder path containing video files (sorted by filename)."),
    ] = None,
    video_paths: Annotated[
        Optional[str],
        Form(description='JSON array of server-side absolute video file paths, e.g. ["C:/a.mp4","C:/b.mp4"]'),
    ] = None,
    video_svc: VideoService = Depends(get_video_service),
    settings: Settings = Depends(get_settings),
):
    # Read uploaded video files from raw form to bypass FastAPI's strict UploadFile
    # validation that rejects empty-string entries sent by Swagger/browsers when
    # video_folder or video_paths is used instead.
    form = await request.form()
    real_videos: list[UploadFile] = [
        v for v in form.getlist("videos")
        if hasattr(v, "filename") and v.filename
    ]

    if not real_videos and not video_folder and not video_paths:
        raise VideoCreationError("Provide one of: video files, video_folder, or video_paths")

    temp_dir = Path(settings.VIDEO_OUTPUT_DIR)
    temp_dir.mkdir(parents=True, exist_ok=True)

    uploaded_video_paths: list[str] = []
    audio_path: Optional[str] = None

    try:
        audio_path = await _save_upload(audio, temp_dir, SUPPORTED_AUDIO_TYPES)

        if video_paths:
            # Server-side paths provided as JSON array string
            try:
                parsed = json.loads(video_paths)
                if not isinstance(parsed, list):
                    raise ValueError("video_paths must be a JSON array")
            except (json.JSONDecodeError, ValueError) as e:
                raise VideoCreationError(f"Invalid video_paths JSON: {e}")
            resolved: list[str] = []
            for p in parsed:
                path = Path(p.strip())
                if not path.is_file():
                    raise VideoCreationError(f"Video file not found: {p}")
                if path.suffix.lower() not in SUPPORTED_VIDEO_TYPES:
                    raise VideoCreationError(f"Unsupported video type '{path.suffix}': {p}")
                resolved.append(str(path))
            logger.info(f"Using {len(resolved)} server-side video path(s)")
            final_video_paths = resolved
        elif video_folder:
            final_video_paths = video_svc.get_video_files_from_folder(video_folder)
            logger.info(f"Using {len(final_video_paths)} video(s) from folder: {video_folder}")
        else:
            for vf in real_videos:
                p = await _save_upload(vf, temp_dir, SUPPORTED_VIDEO_TYPES)
                uploaded_video_paths.append(p)
            final_video_paths = uploaded_video_paths
            logger.info(f"Using {len(final_video_paths)} uploaded video(s)")

        output_path = video_svc.concat_videos_with_audio(final_video_paths, audio_path)
        return FileResponse(
            path=output_path,
            media_type="video/mp4",
            filename=os.path.basename(output_path),
        )
    finally:
        video_svc.cleanup(audio_path, *uploaded_video_paths)
