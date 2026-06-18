import re
from typing import Union


def parse_time_to_seconds(value: Union[str, float, int]) -> float:
    """
    Accept time as:
      - float/int  → treated as seconds (e.g. 90.5)
      - "HH:MM:SS" or "H:MM:SS" string (e.g. "2:04:51", "0:00:30")
      - "MM:SS"    string (e.g. "4:51")
    Returns total seconds as float.
    """
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        parts = value.strip().split(":")
        if len(parts) == 3:
            h, m, s = parts
            return int(h) * 3600 + int(m) * 60 + float(s)
        if len(parts) == 2:
            m, s = parts
            return int(m) * 60 + float(s)
        return float(value)
    raise ValueError(f"Cannot parse time value: {value!r}")


def validate_youtube_url(url: str) -> bool:
    """Validate if the given URL is a valid YouTube URL."""
    youtube_regex = re.compile(
        r"(https?://)?(www\.)?"
        r"(youtube\.com/watch\?v=|youtu\.be/|youtube\.com/shorts/)"
        r"[\w\-]+"
    )
    return bool(youtube_regex.match(url))


def validate_facebook_url(url: str) -> bool:
    """Validate if the given URL is a valid Facebook video URL."""
    facebook_regex = re.compile(
        r"(https?://)?(www\.)?"
        r"(facebook\.com|fb\.watch)/"
        r".+"
    )
    return bool(facebook_regex.match(url))


def validate_douyin_url(url: str) -> bool:
    """Validate if the given URL is a valid Douyin URL."""
    douyin_regex = re.compile(
        r"(https?://)?(www\.)?"
        r"(douyin\.com|v\.douyin\.com)/"
        r".+"
    )
    return bool(douyin_regex.match(url))


def seconds_to_ffmpeg_time(seconds: float) -> str:
    """Convert seconds to ffmpeg time format HH:MM:SS.mmm"""
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = seconds % 60
    return f"{hours:02d}:{minutes:02d}:{secs:06.3f}"
