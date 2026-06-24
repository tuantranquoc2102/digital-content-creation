from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, model_validator


class SocialPlatform(str, Enum):
    facebook = "facebook"
    instagram = "instagram"
    youtube = "youtube"
    tiktok = "tiktok"


class SocialVideoType(str, Enum):
    video = "video"
    short_video = "short_video"


class SocialPublishRequest(BaseModel):
    platform: SocialPlatform = Field(description="Target social platform")
    video_type: SocialVideoType = Field(
        default=SocialVideoType.video,
        description="Use short_video for reels/shorts style posts",
    )
    title: Optional[str] = Field(default=None, description="Optional post title")
    caption: Optional[str] = Field(default=None, description="Post caption/description")

    # Input source
    video_url: Optional[str] = Field(
        default=None,
        description="Publicly reachable video URL. Required for Instagram Reels.",
    )
    video_file_path: Optional[str] = Field(
        default=None,
        description="Absolute or relative local path on server machine.",
    )

    # Optional credential overrides (fallback to .env if omitted)
    access_token: Optional[str] = None
    page_id: Optional[str] = None
    instagram_user_id: Optional[str] = None

    publish_now: bool = Field(
        default=True,
        description="If false, platform may create an unpublished/draft video depending on API support.",
    )

    @model_validator(mode="after")
    def validate_input_source(self) -> "SocialPublishRequest":
        if not self.video_url and not self.video_file_path:
            raise ValueError("Either video_url or video_file_path is required")
        return self


class SocialPublishResponse(BaseModel):
    platform: SocialPlatform
    video_type: SocialVideoType
    status: str
    post_id: Optional[str] = None
    creation_id: Optional[str] = None
    message: str
    raw_response: dict = Field(default_factory=dict)
