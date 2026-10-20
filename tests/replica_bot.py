"""Override only Telegram's network transport in local/CI replica checks."""

import os

from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer

from bot import main
from tests import telegram_stub


def test_bot(token):
    return Bot(
        token,
        session=AiohttpSession(
            api=TelegramAPIServer.from_base(
                os.getenv("TELEGRAM_TEST_API_BASE", "http://127.0.0.1:58090")
            )
        ),
    )


main.Bot = test_bot
app = main.app
app.mount("/telegram-api", telegram_stub.app)
