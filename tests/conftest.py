import base64
import io
import os

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy.engine import make_url

from backend import main


@pytest.fixture
def photo():
    buffer = io.BytesIO()
    Image.new("RGB", (8, 8), "white").save(buffer, format="PNG")
    return buffer.getvalue()


@pytest.fixture
def payload(photo):
    return {
        "telegram_update_id": 1001,
        "title": "Белая чашка",
        "image_base64": base64.b64encode(photo).decode(),
    }


@pytest.fixture
def api(monkeypatch):
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set TEST_DATABASE_URL to a dedicated PostgreSQL catalog_test database")
    if make_url(url).database != "catalog_test":
        pytest.fail("Tests require a dedicated catalog_test database")
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("BOT_API_SECRET", "t" * 40)
    monkeypatch.setenv("MINIO_ACCESS_KEY", os.getenv("TEST_MINIO_ACCESS_KEY", "catalog-test"))
    monkeypatch.setenv(
        "MINIO_SECRET_KEY", os.getenv("TEST_MINIO_SECRET_KEY", "catalog-test-password")
    )
    monkeypatch.setenv("MINIO_ENDPOINT", os.getenv("TEST_MINIO_ENDPOINT", "127.0.0.1:59000"))
    monkeypatch.setenv("MINIO_BUCKET", "catalog-test")
    main.settings.cache_clear()
    from backend.init_storage import main as init_storage

    init_storage()
    with TestClient(main.app) as client:
        from sqlalchemy import text

        async def reset():
            async with main.app.state.sessions() as db:
                await db.execute(text("TRUNCATE products RESTART IDENTITY"))
                await db.commit()

        client.portal.call(reset)
        client.headers["X-Bot-Secret"] = "t" * 40
        yield client
    main.settings.cache_clear()
