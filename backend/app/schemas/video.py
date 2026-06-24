from pydantic import BaseModel, Field, field_validator


class YouTubeVideoDownloadRequest(BaseModel):
    url: str

    @field_validator("url")
    @classmethod
    def url_must_be_youtube(cls, v: str) -> str:
        from app.core.utils import validate_youtube_url

        if not validate_youtube_url(v):
            raise ValueError("Must be a valid YouTube URL")
        return v


class FacebookVideoDownloadRequest(BaseModel):
    url: str

    @field_validator("url")
    @classmethod
    def url_must_be_facebook(cls, v: str) -> str:
        from app.core.utils import validate_facebook_url

        if not validate_facebook_url(v):
            raise ValueError("Must be a valid Facebook URL")
        return v


class DouyinVideoDownloadRequest(BaseModel):
    url: str

    @field_validator("url")
    @classmethod
    def url_must_be_douyin(cls, v: str) -> str:
        from app.core.utils import validate_douyin_url

        if not validate_douyin_url(v):
            raise ValueError("Must be a valid Douyin URL")
        return v


class FacebookProfileDownloadRequest(BaseModel):
    url: str
    max_videos: int = Field(
        default=10,
        ge=1,
        le=100,
        description="Maximum number of videos to download (1–100). Defaults to 10.",
    )

    @field_validator("url")
    @classmethod
    def url_must_be_facebook(cls, v: str) -> str:
        from app.core.utils import validate_facebook_url

        if not validate_facebook_url(v):
            raise ValueError("Must be a valid Facebook URL")
        return v