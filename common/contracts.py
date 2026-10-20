import base64
import binascii
import io
import warnings

from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel, ConfigDict, Field, field_validator

MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_JSON_BYTES = 4 * ((MAX_IMAGE_BYTES + 2) // 3) + 4096
Image.MAX_IMAGE_PIXELS = 20_000_000


class ProductCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    telegram_update_id: int = Field(ge=0, le=2**63 - 1, strict=True)
    title: str = Field(min_length=1, max_length=200)
    image_base64: str = Field(min_length=4, max_length=4 * ((MAX_IMAGE_BYTES + 2) // 3))

    @field_validator("title")
    @classmethod
    def clean_title(cls, value: str) -> str:
        if any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError("Название не должно содержать управляющие символы")
        return value


def decode_image(encoded: str) -> tuple[bytes, str]:
    try:
        data = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError("Некорректный base64") from exc
    if not data or len(data) > MAX_IMAGE_BYTES:
        raise ValueError("Максимальный размер фото — 5 МиБ")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                image_format = image.format
                if image_format not in {"JPEG", "PNG", "WEBP"}:
                    raise ValueError("Разрешены JPEG, PNG и WebP")
                image.verify()
            with Image.open(io.BytesIO(data)) as image:
                image.load()
    except (
        UnidentifiedImageError,
        OSError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ) as exc:
        raise ValueError("Повреждённое или слишком большое изображение") from exc
    return data, {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}[image_format]


class BodyLimitMiddleware:
    """Bound the actual streamed body, including requests without Content-Length."""

    def __init__(self, app, limit: int):
        self.app, self.limit = app, limit

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] not in {"POST", "PUT", "PATCH"}:
            return await self.app(scope, receive, send)
        parts, size = [], 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            size += len(message.get("body", b""))
            if size > self.limit:
                await send({"type": "http.response.start", "status": 413, "headers": []})
                await send({"type": "http.response.body", "body": b"Request too large"})
                return
            parts.append(message.get("body", b""))
            if not message.get("more_body", False):
                break
        delivered = False

        async def replay():
            nonlocal delivered
            if delivered:
                return await receive()
            delivered = True
            return {"type": "http.request", "body": b"".join(parts), "more_body": False}

        await self.app(scope, replay, send)
