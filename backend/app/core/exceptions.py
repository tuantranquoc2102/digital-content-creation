from fastapi import HTTPException, status


class AudioDownloadError(HTTPException):
    def __init__(self, detail: str):
        super().__init__(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Audio download failed: {detail}",
        )


class TranscriptionError(HTTPException):
    def __init__(self, detail: str):
        super().__init__(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Transcription failed: {detail}",
        )


class InvalidTimeRangeError(HTTPException):
    def __init__(self, detail: str = "Invalid time range: start_time must be less than end_time"):
        super().__init__(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=detail,
        )


class InvalidYouTubeURLError(HTTPException):
    def __init__(self):
        super().__init__(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid YouTube URL provided",
        )


class VideoCreationError(HTTPException):
    def __init__(self, detail: str):
        super().__init__(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Video creation failed: {detail}",
        )


class VideoDownloadError(HTTPException):
    def __init__(self, detail: str):
        super().__init__(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Video download failed: {detail}",
        )


class UnsupportedFileTypeError(HTTPException):
    def __init__(self, received: str, allowed: set[str]):
        super().__init__(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=(
                f"Unsupported file type '{received}'. "
                f"Allowed: {', '.join(sorted(allowed))}"
            ),
        )


class TTSError(HTTPException):
    def __init__(self, detail: str):
        super().__init__(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"TTS failed: {detail}",
        )


class SocialPublishError(HTTPException):
    def __init__(self, detail: str):
        super().__init__(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Social publish failed: {detail}",
        )
