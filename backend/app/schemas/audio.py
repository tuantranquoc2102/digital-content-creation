from pydantic import BaseModel, field_validator, model_validator
from typing import Optional, Union


TimeInput = Union[float, int, str]


def _to_seconds(value: Optional[TimeInput]) -> Optional[float]:
    if value is None:
        return None
    from app.core.utils import parse_time_to_seconds
    return parse_time_to_seconds(value)


class AudioSegment(BaseModel):
    """A single time-bounded segment of an audio file."""

    start_time: TimeInput
    end_time: TimeInput
    label: Optional[str] = None  # used as filename hint in the output ZIP

    @field_validator("start_time", "end_time", mode="before")
    @classmethod
    def parse_time(cls, v: Optional[TimeInput]) -> Optional[float]:
        return _to_seconds(v)

    @model_validator(mode="after")
    def validate_range(self) -> "AudioSegment":
        if self.end_time <= self.start_time:
            raise ValueError("end_time must be greater than start_time")
        return self


class YouTubeAudioRequest(BaseModel):
    url: str
    start_time: Optional[TimeInput] = None
    end_time: Optional[TimeInput] = None

    @field_validator("url")
    @classmethod
    def url_must_be_youtube(cls, v: str) -> str:
        from app.core.utils import validate_youtube_url
        if not validate_youtube_url(v):
            raise ValueError("Must be a valid YouTube URL")
        return v

    @field_validator("start_time", "end_time", mode="before")
    @classmethod
    def parse_time(cls, v: Optional[TimeInput]) -> Optional[float]:
        return _to_seconds(v)

    @model_validator(mode="after")
    def validate_time_range(self) -> "YouTubeAudioRequest":
        effective_start = 0.0 if self.start_time is None else self.start_time
        if self.end_time is not None and self.end_time <= effective_start:
            raise ValueError("end_time must be greater than start_time")
        return self


class YouTubeTranscriptionRequest(BaseModel):
    url: str
    start_time: TimeInput = 0.0
    end_time: Optional[TimeInput] = None
    language: Optional[str] = None

    @field_validator("url")
    @classmethod
    def url_must_be_youtube(cls, v: str) -> str:
        from app.core.utils import validate_youtube_url
        if not validate_youtube_url(v):
            raise ValueError("Must be a valid YouTube URL")
        return v

    @field_validator("start_time", "end_time", mode="before")
    @classmethod
    def parse_time(cls, v: Optional[TimeInput]) -> Optional[float]:
        return _to_seconds(v)

    @model_validator(mode="after")
    def validate_time_range(self) -> "YouTubeTranscriptionRequest":
        if self.end_time is not None and self.end_time <= self.start_time:
            raise ValueError("end_time must be greater than start_time")
        return self


class TranscriptionResponse(BaseModel):
    text: str
    language: Optional[str] = None
    segments: Optional[list] = None
    duration: Optional[float] = None
