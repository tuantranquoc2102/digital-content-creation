from fastapi import APIRouter, Depends

from app.schemas.social import SocialPublishRequest, SocialPublishResponse
from app.services.social_publish_service import SocialPublishService

router = APIRouter(prefix="/social", tags=["Social"])


def get_social_publish_service() -> SocialPublishService:
    return SocialPublishService()


@router.post(
    "/publish",
    response_model=SocialPublishResponse,
    summary="Publish video/short-video to social platforms",
    description=(
        "Publish video content to social platforms using API credentials from .env. "
        "Currently supported platforms: facebook, instagram. "
        "For Instagram, provide a public `video_url`."
    ),
)
async def publish_social_video(
    request: SocialPublishRequest,
    social_svc: SocialPublishService = Depends(get_social_publish_service),
):
    return social_svc.publish(request)
