from fastapi import APIRouter

from app.api.v1.routes import audio, video, tts

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(audio.router)
api_router.include_router(video.router)
api_router.include_router(tts.router)
