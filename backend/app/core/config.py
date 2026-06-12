from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    APP_NAME: str = "Digital Content Creation API"
    APP_VERSION: str = "1.0.0"
    DEBUG: bool = False

    # Audio settings
    AUDIO_OUTPUT_DIR: str = "temp/audio"
    MAX_AUDIO_DURATION_SECONDS: int = 3600  # 1 hour

    # Video settings
    VIDEO_OUTPUT_DIR: str = "temp/video"
    VIDEO_FONTS_DIR: str = "static/fonts"  # Server-side directory for ticker fonts
    VIDEO_DEFAULT_FONT_FILENAME: str = "UVNMangCau_B.ttf"

    # TTS settings
    TTS_OUTPUT_DIR: str = "temp/tts"
    TTS_HEADLESS: bool = True  # Set False to watch the browser during TTS
    TTS_CHUNK_SIZE: int = 5000  # Max chars per TTS request chunk (~1000 chars per submit)
    TTS_CHUNK_MAX_RETRIES: int = 3   # Per-chunk retry attempts
    TTS_CHUNK_RETRY_DELAY_S: int = 5  # Base delay between retries (seconds)

    # Transcription settings
    WHISPER_MODEL: str = "base"  # tiny, base, small, medium, large
    WHISPER_DEVICE: str = "cpu"  # cpu or cuda

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


@lru_cache()
def get_settings() -> Settings:
    return Settings()
