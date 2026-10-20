import io
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import TelegramNetworkError
from aiogram.methods import SendPhoto
from aiogram.types import Message, Update
from fastapi import HTTPException
from fastapi.testclient import TestClient

from bot import main


@pytest.fixture
def config(monkeypatch):
    for key, value in {
        "BOT_TOKEN": "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijk",
        "TELEGRAM_ADMIN_ID": "42",
        "TELEGRAM_WEBHOOK_SECRET": "s" * 40,
        "BOT_API_SECRET": "t" * 40,
        "MINI_APP_URL": "https://catalog.example.com",
    }.items():
        monkeypatch.setenv(key, value)
    return main.Settings()


@pytest.fixture
def message():
    return SimpleNamespace(
        from_user=SimpleNamespace(id=42),
        chat=SimpleNamespace(type="private"),
        photo=[SimpleNamespace(file_id="photo", file_size=50)],
        caption="Чашка",
        answer=AsyncMock(),
        answer_photo=AsyncMock(),
    )


async def test_admin_check_before_download(config, message):
    message.from_user.id = 13
    bot, backend = SimpleNamespace(download=AsyncMock()), SimpleNamespace(post=AsyncMock())
    await main.add_product(message, bot, config, backend, 1)
    bot.download.assert_not_called()
    backend.post.assert_not_called()


async def test_missing_caption(config, message):
    message.caption = " "
    bot = SimpleNamespace(download=AsyncMock())
    await main.add_product(message, bot, config, SimpleNamespace(), 1)
    bot.download.assert_not_called()
    message.answer.assert_awaited_once()


async def test_confirmation_only_for_creation(config, message, photo):
    async def download(_photo, destination, **_kwargs):
        destination.write(photo)

    backend = SimpleNamespace(
        post=AsyncMock(
            side_effect=[
                httpx.Response(
                    201,
                    json={"created": True},
                    request=httpx.Request("POST", "http://api/products"),
                ),
                httpx.Response(
                    200,
                    json={"created": False},
                    request=httpx.Request("POST", "http://api/products"),
                ),
            ]
        )
    )
    bot = SimpleNamespace(download=download)
    for _ in range(2):
        await main.add_product(message, bot, config, backend, 1)
    message.answer_photo.assert_awaited_once_with("photo", caption="Добавлено: Чашка")


async def test_backend_failure_not_acknowledged(config, message, photo):
    async def download(_photo, destination, **_kwargs):
        destination.write(photo)

    backend = SimpleNamespace(post=AsyncMock(side_effect=httpx.ConnectError("offline")))
    with pytest.raises(HTTPException) as error:
        await main.add_product(message, SimpleNamespace(download=download), config, backend, 1)
    assert error.value.status_code == 503
    message.answer_photo.assert_not_called()


@pytest.mark.parametrize("status", [409, 422])
async def test_permanent_rejection_informs_admin(config, message, photo, status):
    async def download(_photo, destination, **_kwargs):
        destination.write(photo)

    backend = SimpleNamespace(
        post=AsyncMock(
            return_value=httpx.Response(
                status, request=httpx.Request("POST", "http://api/products")
            )
        )
    )
    await main.add_product(message, SimpleNamespace(download=download), config, backend, 1)
    message.answer.assert_awaited_once()
    message.answer_photo.assert_not_called()


def test_buffer_is_bounded():
    buffer = main.LimitedBuffer()
    buffer.seek(main.MAX_IMAGE_BYTES)
    with pytest.raises(ValueError):
        buffer.write(b"x")
    assert isinstance(buffer, io.BytesIO)


def test_webhook_secret_and_real_dispatcher(config, monkeypatch):
    send = AsyncMock(
        return_value=Message(
            message_id=2, date=datetime.now(UTC), chat={"id": 42, "type": "private"}
        )
    )
    monkeypatch.setattr(AiohttpSession, "make_request", send)
    with TestClient(main.app) as client:
        update = Update(
            update_id=4,
            message=Message(
                message_id=1,
                date=datetime.now(UTC),
                chat={"id": 42, "type": "private"},
                from_user={"id": 42, "is_bot": False, "first_name": "Admin"},
                text="/start",
                entities=[{"type": "bot_command", "offset": 0, "length": 6}],
            ),
        ).model_dump(mode="json")
        assert client.post("/telegram/webhook", json=update).status_code == 401
        response = client.post(
            "/telegram/webhook", json=update, headers={"X-Telegram-Bot-Api-Secret-Token": "s" * 40}
        )
        assert response.status_code == 200, response.text
    send.assert_awaited_once()


def test_webhook_propagates_backend_failure(config, photo, monkeypatch):
    async def download(_self, _photo, destination, **_kwargs):
        destination.write(photo)

    monkeypatch.setattr(Bot, "download", download)
    with TestClient(main.app) as client:
        monkeypatch.setattr(
            main.app.state.backend,
            "post",
            AsyncMock(
                return_value=httpx.Response(
                    503, request=httpx.Request("POST", "http://api/products")
                )
            ),
        )
        update = {
            "update_id": 10,
            "message": {
                "message_id": 1,
                "date": 1700000000,
                "chat": {"id": 42, "type": "private"},
                "from": {"id": 42, "is_bot": False, "first_name": "Admin"},
                "caption": "Чашка",
                "photo": [
                    {"file_id": "photo", "file_unique_id": "unique", "width": 8, "height": 8}
                ],
            },
        }
        response = client.post(
            "/telegram/webhook", json=update, headers={"X-Telegram-Bot-Api-Secret-Token": "s" * 40}
        )
        assert response.status_code == 503


@pytest.mark.parametrize("lost_reply", ["backend", "telegram"])
def test_persisted_product_without_confirmation_is_not_resent(
    api, config, photo, monkeypatch, lost_reply
):
    async def download(_self, _photo, destination, **_kwargs):
        destination.write(photo)

    async def persist(path, json):
        response = api.post(path, json=json)
        if lost_reply == "backend" and response.status_code == 201:
            raise httpx.ReadError("Backend response lost after COMMIT")
        return httpx.Response(
            response.status_code,
            json=response.json(),
            request=httpx.Request("POST", "http://api/products"),
        )

    send_photo = AsyncMock(
        side_effect=TelegramNetworkError(
            method=SendPhoto(chat_id=42, photo="photo"), message="Confirmation response lost"
        )
    )
    monkeypatch.setattr(Bot, "download", download)
    monkeypatch.setattr(AiohttpSession, "make_request", send_photo)
    with TestClient(main.app) as client:
        monkeypatch.setattr(main.app.state.backend, "post", persist)
        update = {
            "update_id": 11,
            "message": {
                "message_id": 1,
                "date": 1700000000,
                "chat": {"id": 42, "type": "private"},
                "from": {"id": 42, "is_bot": False, "first_name": "Admin"},
                "caption": "Сохранено без подтверждения",
                "photo": [
                    {"file_id": "photo", "file_unique_id": "unique", "width": 8, "height": 8}
                ],
            },
        }
        headers = {"X-Telegram-Bot-Api-Secret-Token": "s" * 40}
        first = client.post("/telegram/webhook", json=update, headers=headers)
        assert first.status_code == (503 if lost_reply == "backend" else 200)
        assert len(api.get("/products").json()) == 1
        assert client.post("/telegram/webhook", json=update, headers=headers).status_code == 200
        assert len(api.get("/products").json()) == 1
    assert send_photo.await_count == (0 if lost_reply == "backend" else 1)
