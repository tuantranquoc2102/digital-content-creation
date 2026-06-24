import time
from pathlib import Path

import requests

from app.core.config import get_settings
from app.core.exceptions import SocialPublishError
from app.schemas.social import SocialPlatform, SocialPublishRequest, SocialPublishResponse

settings = get_settings()


class SocialPublishService:
    """Publish video/short-video content to social platforms."""

    def publish(self, request: SocialPublishRequest) -> SocialPublishResponse:
        if request.platform == SocialPlatform.facebook:
            return self._publish_to_facebook(request)
        if request.platform == SocialPlatform.instagram:
            return self._publish_to_instagram(request)

        raise SocialPublishError(
            f"Platform '{request.platform.value}' is not implemented yet. "
            "Currently supported: facebook, instagram"
        )

    def _publish_to_facebook(self, request: SocialPublishRequest) -> SocialPublishResponse:
        page_id = request.page_id or settings.FACEBOOK_PAGE_ID
        access_token = request.access_token or settings.FACEBOOK_PAGE_ACCESS_TOKEN

        if not page_id:
            raise SocialPublishError("Missing FACEBOOK_PAGE_ID (or request.page_id)")
        if not access_token:
            raise SocialPublishError(
                "Missing FACEBOOK_PAGE_ACCESS_TOKEN (or request.access_token)"
            )

        endpoint = (
            f"https://graph.facebook.com/{settings.FACEBOOK_GRAPH_VERSION}/{page_id}/videos"
        )
        payload = {
            "access_token": access_token,
            "description": request.caption or request.title or "",
            "published": "true" if request.publish_now else "false",
        }

        try:
            if request.video_url:
                payload["file_url"] = request.video_url
                response = requests.post(
                    endpoint,
                    data=payload,
                    timeout=settings.SOCIAL_HTTP_TIMEOUT_SECONDS,
                )
            else:
                video_path = Path(request.video_file_path or "").expanduser()
                if not video_path.exists() or not video_path.is_file():
                    raise SocialPublishError(
                        f"video_file_path does not exist or is not a file: {request.video_file_path}"
                    )

                with open(video_path, "rb") as video_file:
                    response = requests.post(
                        endpoint,
                        data=payload,
                        files={"source": video_file},
                        timeout=settings.SOCIAL_HTTP_TIMEOUT_SECONDS,
                    )
        except requests.RequestException as exc:
            raise SocialPublishError(f"Facebook API request failed: {exc}")

        data = self._parse_json_response(response, "Facebook")
        self._raise_if_api_error(data, "Facebook")

        post_id = data.get("id")
        if not post_id:
            raise SocialPublishError("Facebook API returned no video ID")

        return SocialPublishResponse(
            platform=request.platform,
            video_type=request.video_type,
            status="published" if request.publish_now else "uploaded",
            post_id=post_id,
            message="Video submitted to Facebook successfully",
            raw_response=data,
        )

    def _publish_to_instagram(self, request: SocialPublishRequest) -> SocialPublishResponse:
        ig_user_id = request.instagram_user_id or settings.INSTAGRAM_USER_ID
        access_token = request.access_token or settings.INSTAGRAM_ACCESS_TOKEN

        if not ig_user_id:
            raise SocialPublishError(
                "Missing INSTAGRAM_USER_ID (or request.instagram_user_id)"
            )
        if not access_token:
            raise SocialPublishError(
                "Missing INSTAGRAM_ACCESS_TOKEN (or request.access_token)"
            )
        if not request.video_url:
            raise SocialPublishError(
                "Instagram publishing requires video_url (public HTTPS URL)"
            )

        create_endpoint = (
            f"https://graph.facebook.com/{settings.INSTAGRAM_GRAPH_VERSION}/{ig_user_id}/media"
        )
        create_payload = {
            "access_token": access_token,
            "media_type": "REELS" if request.video_type.value == "short_video" else "VIDEO",
            "video_url": request.video_url,
            "caption": request.caption or request.title or "",
            "share_to_feed": "true",
        }

        try:
            create_response = requests.post(
                create_endpoint,
                data=create_payload,
                timeout=settings.SOCIAL_HTTP_TIMEOUT_SECONDS,
            )
        except requests.RequestException as exc:
            raise SocialPublishError(f"Instagram create-media request failed: {exc}")

        create_data = self._parse_json_response(create_response, "Instagram")
        self._raise_if_api_error(create_data, "Instagram")
        creation_id = create_data.get("id")
        if not creation_id:
            raise SocialPublishError("Instagram API returned no creation ID")

        self._poll_instagram_media_status(creation_id, access_token)

        publish_endpoint = (
            f"https://graph.facebook.com/{settings.INSTAGRAM_GRAPH_VERSION}/{ig_user_id}/media_publish"
        )
        publish_payload = {
            "access_token": access_token,
            "creation_id": creation_id,
        }

        try:
            publish_response = requests.post(
                publish_endpoint,
                data=publish_payload,
                timeout=settings.SOCIAL_HTTP_TIMEOUT_SECONDS,
            )
        except requests.RequestException as exc:
            raise SocialPublishError(f"Instagram publish request failed: {exc}")

        publish_data = self._parse_json_response(publish_response, "Instagram")
        self._raise_if_api_error(publish_data, "Instagram")
        post_id = publish_data.get("id")

        if not post_id:
            raise SocialPublishError("Instagram API returned no media ID after publish")

        return SocialPublishResponse(
            platform=request.platform,
            video_type=request.video_type,
            status="published",
            post_id=post_id,
            creation_id=creation_id,
            message="Video submitted to Instagram successfully",
            raw_response=publish_data,
        )

    def _poll_instagram_media_status(self, creation_id: str, access_token: str) -> None:
        endpoint = (
            f"https://graph.facebook.com/{settings.INSTAGRAM_GRAPH_VERSION}/{creation_id}"
        )
        params = {
            "fields": "status,status_code,error_message",
            "access_token": access_token,
        }

        for _ in range(settings.SOCIAL_POLL_MAX_ATTEMPTS):
            try:
                response = requests.get(
                    endpoint,
                    params=params,
                    timeout=settings.SOCIAL_HTTP_TIMEOUT_SECONDS,
                )
            except requests.RequestException as exc:
                raise SocialPublishError(f"Instagram status polling failed: {exc}")

            data = self._parse_json_response(response, "Instagram")
            self._raise_if_api_error(data, "Instagram")

            status_code = (data.get("status_code") or "").upper()
            status = (data.get("status") or "").upper()

            if status_code == "FINISHED" or status == "FINISHED":
                return
            if status_code in {"ERROR", "EXPIRED"} or status in {"ERROR", "EXPIRED"}:
                error_message = data.get("error_message") or "Instagram processing failed"
                raise SocialPublishError(error_message)

            time.sleep(settings.SOCIAL_POLL_INTERVAL_SECONDS)

        raise SocialPublishError(
            "Instagram media processing timed out. Try again later or increase polling settings."
        )

    @staticmethod
    def _parse_json_response(response: requests.Response, provider: str) -> dict:
        try:
            return response.json()
        except ValueError:
            raise SocialPublishError(
                f"{provider} API returned non-JSON response (HTTP {response.status_code})"
            )

    @staticmethod
    def _raise_if_api_error(data: dict, provider: str) -> None:
        error = data.get("error")
        if not error:
            return

        message = error.get("message") or str(error)
        code = error.get("code")
        raise SocialPublishError(f"{provider} API error ({code}): {message}")
