import base64
import io

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

from common.contracts import MAX_IMAGE_BYTES, BodyLimitMiddleware, decode_image


@pytest.mark.parametrize("image_format", ["JPEG", "PNG", "WEBP"])
def test_image_formats(image_format):
    buffer = io.BytesIO()
    Image.new("RGB", (4, 4), "white").save(buffer, format=image_format)
    data, mime = decode_image(base64.b64encode(buffer.getvalue()).decode())
    assert data == buffer.getvalue()
    assert mime.startswith("image/")


def test_oversize_and_svg():
    for data in [b"x" * (MAX_IMAGE_BYTES + 1), b'<svg onload="alert(1)"/>']:
        with pytest.raises(ValueError):
            decode_image(base64.b64encode(data).decode())


def test_chunked_body_limit():
    app = FastAPI()
    app.add_middleware(BodyLimitMiddleware, limit=16)

    @app.post("/")
    async def endpoint():
        return {"ok": True}

    with TestClient(app) as client:
        assert client.post("/", content=(b"0123456789" for _ in range(3))).status_code == 413
        assert client.post("/", content=b"small").status_code == 200
