# Backend — Digital Content Creation API

FastAPI backend for downloading media and generating transcriptions.

## Requirements

- Python 3.10+
- [FFmpeg](https://ffmpeg.org/download.html) must be installed and available in `PATH`

## Setup

```bash
cd backend

# Create virtual environment
python -m venv .venv
.venv\Scripts\activate        # Windows
# source .venv/bin/activate   # macOS/Linux

# Install dependencies
pip install -r requirements.txt

# Copy environment file
copy .env.example .env
```

## Run

```bash
python run.py
```

Swagger UI: http://localhost:8000/docs

---

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/v1/audio/download` | Download audio from YouTube (with optional trim) |
| `POST` | `/api/v1/audio/transcribe/youtube` | Transcribe audio from YouTube link |
| `POST` | `/api/v1/audio/transcribe/file` | Transcribe uploaded audio file |
| `POST` | `/api/v1/video/download/youtube` | Download video from YouTube |
| `POST` | `/api/v1/video/download/facebook` | Download video from Facebook |
| `POST` | `/api/v1/video/download/douyin` | Download video from Douyin |
| `GET`  | `/health` | Health check |

---

## Endpoint Details

### POST `/api/v1/audio/download`

Download trimmed MP3 audio from a YouTube link.

**Request body (JSON):**
```json
{
  "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
  "start_time": 30.0,
  "end_time": 90.0
}
```

Returns: `audio/mpeg` file download.

---

### POST `/api/v1/audio/transcribe/youtube`

Download and transcribe audio from a YouTube link.

**Request body (JSON):**
```json
{
  "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
  "start_time": 0.0,
  "end_time": 60.0,
  "language": "en"
}
```

Returns:
```json
{
  "text": "Full transcript...",
  "language": "en",
  "segments": [
    { "id": 0, "start": 0.0, "end": 5.0, "text": "Hello world" }
  ],
  "duration": 60.0
}
```

---

### POST `/api/v1/audio/transcribe/file`

Upload an audio file and receive its transcription.

**Form data:**
- `file` — audio file (mp3, wav, m4a, ogg, flac…)
- `language` *(optional)* — ISO-639-1 code (e.g. `en`, `vi`)

Returns: same shape as transcribe/youtube.

---

### POST `/api/v1/video/download/youtube`

Download a single video from YouTube.

**Request body (JSON):**
```json
{
  "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
}
```

Returns: `video/mp4` file download.

---

### POST `/api/v1/video/download/facebook`

Download a single video from Facebook.

**Request body (JSON):**
```json
{
  "url": "https://www.facebook.com/watch/?v=1234567890"
}
```

Returns: `video/mp4` file download.

---

### POST `/api/v1/video/download/douyin`

Download a single video from Douyin.

For playback compatibility, the backend auto-converts Douyin output to H.264/AAC MP4 when needed. This avoids requiring HEVC/H.265 extensions on Windows players.

**Request body (JSON):**
```json
{
  "url": "https://www.douyin.com/video/1234567890"
}
```

Returns: `video/mp4` file download.

---

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `WHISPER_MODEL` | `base` | Whisper model size: `tiny`, `base`, `small`, `medium`, `large` |
| `WHISPER_DEVICE` | `cpu` | `cpu` or `cuda` |
| `AUDIO_OUTPUT_DIR` | `temp/audio` | Temporary directory for downloaded audio |
