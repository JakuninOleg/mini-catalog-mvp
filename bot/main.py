import base64
import io
import logging
import secrets
from contextlib import asynccontextmanager
from typing import Annotated

import httpx
from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import CommandStart
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message, Update, WebAppInfo
from fastapi import FastAPI, Header, HTTPException
from pydantic import Field, HttpUrl, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from common.contracts import MAX_IMAGE_BYTES, BodyLimitMiddleware, ProductCreate

logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    bot_token: SecretStr
    telegram_admin_id: int = Field(gt=0)
    telegram_webhook_secret: SecretStr = Field(min_length=32, max_length=256)
    bot_api_secret: SecretStr = Field(min_length=32)
    backend_url: HttpUrl = "http://backend:8000"
    mini_app_url: HttpUrl

    @field_validator("telegram_webhook_secret")
    @classmethod
    def valid_secret(cls, value: SecretStr):
        if not all(c.isascii() and (c.isalnum() or c in "_-") for c in value.get_secret_value()):
            raise ValueError("Webhook secret must use A-Z, a-z, 0-9, _ or -")
        return value

    @field_validator("mini_app_url")
    @classmethod
    def https_url(cls, value: HttpUrl):
        if value.scheme != "https":
            raise ValueError("Telegram Mini App requires HTTPS")
        return value


class LimitedBuffer(io.BytesIO):
    def write(self, data):
        if self.tell() + len(data) > MAX_IMAGE_BYTES:
            raise ValueError("Максимальный размер фото — 5 МиБ")
        return super().write(data)


dp = Dispatcher(disable_fsm=True)


@dp.message(CommandStart())
async def start(message: Message, config: Settings):
    if message.chat.type != "private":
        return
    await message.answer(
        "Откройте каталог. Администратор может добавить товар: фото с названием в подписи.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="Каталог", web_app=WebAppInfo(url=str(config.mini_app_url))
                    )
                ]
            ]
        ),
    )


@dp.message(F.photo)
async def add_product(
    message: Message, bot: Bot, config: Settings, backend: httpx.AsyncClient, update_id: int
):
    if (
        message.from_user is None
        or message.from_user.id != config.telegram_admin_id
        or message.chat.type != "private"
    ):
        return
    try:
        title = (message.caption or "").strip()
        photo = message.photo[-1]
        if photo.file_size is not None and photo.file_size > MAX_IMAGE_BYTES:
            raise ValueError("Максимальный размер фото — 5 МиБ")
        # Validate caption before any Telegram file download.
        ProductCreate(telegram_update_id=update_id, title=title, image_base64="AAAA")
    except ValueError:
        await message.answer("Пришлите фото до 5 МиБ с названием от 1 до 200 символов в подписи.")
        return
    data = LimitedBuffer()
    try:
        await bot.download(photo, destination=data, timeout=15)
    except ValueError:
        await message.answer("Максимальный размер фото — 5 МиБ.")
        return
    except TelegramBadRequest as exc:
        logger.warning("Telegram rejected file download: %s", type(exc).__name__)
        await message.answer("Не удалось получить фото. Пришлите его ещё раз.")
        return
    payload = ProductCreate(
        telegram_update_id=update_id,
        title=title,
        image_base64=base64.b64encode(data.getvalue()).decode("ascii"),
    )
    try:
        result = await backend.post("/products", json=payload.model_dump())
        if result.status_code in {409, 422}:
            logger.warning("Product rejected: HTTP %s", result.status_code)
            await message.answer(
                "Это событие уже записано с другим содержимым. Пришлите товар новым сообщением."
                if result.status_code == 409
                else "Фото не прошло проверку. Пришлите другое фото в формате JPEG, PNG или WebP."
            )
            return
        result.raise_for_status()
    except httpx.HTTPError as exc:
        # Let Telegram retry: no successful webhook response before durable persistence.
        raise HTTPException(503, "Backend unavailable") from exc
    if result.json()["created"]:
        # A saved product can remain without confirmation if this attempt fails.
        try:
            await message.answer_photo(photo.file_id, caption=f"Добавлено: {title}")
        except TelegramAPIError as exc:
            logger.warning("Confirmation delivery unknown: %s", type(exc).__name__)


@dp.message()
async def instructions(message: Message, config: Settings):
    if message.from_user and message.from_user.id == config.telegram_admin_id:
        await message.answer("Пришлите фотографию товара и название в подписи к ней.")


@asynccontextmanager
async def lifespan(app: FastAPI):
    config = Settings()
    app.state.config = config
    app.state.bot = Bot(config.bot_token.get_secret_value())
    app.state.backend = httpx.AsyncClient(
        base_url=str(config.backend_url),
        timeout=40,
        trust_env=False,
        headers={"X-Bot-Secret": config.bot_api_secret.get_secret_value()},
    )
    yield
    await app.state.backend.aclose()
    await app.state.bot.session.close()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None)
app.add_middleware(BodyLimitMiddleware, limit=256 * 1024)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/telegram/webhook")
async def webhook(
    update: Update, x_telegram_bot_api_secret_token: Annotated[str | None, Header()] = None
):
    if not x_telegram_bot_api_secret_token or not secrets.compare_digest(
        x_telegram_bot_api_secret_token.encode(),
        app.state.config.telegram_webhook_secret.get_secret_value().encode(),
    ):
        raise HTTPException(401, "Unauthorized")
    await dp.feed_update(
        app.state.bot,
        update,
        config=app.state.config,
        backend=app.state.backend,
        update_id=update.update_id,
    )
    return {"ok": True}
