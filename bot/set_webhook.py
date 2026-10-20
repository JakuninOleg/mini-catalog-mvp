import asyncio
import sys

from aiogram import Bot

from bot.main import Settings


async def main():
    config = Settings()
    url = sys.argv[1] if len(sys.argv) == 2 else ""
    if not url.startswith("https://") or not url.endswith("/telegram/webhook"):
        raise SystemExit("Usage: python -m bot.set_webhook https://HOST/telegram/webhook")
    async with Bot(config.bot_token.get_secret_value()) as bot:
        await bot.set_webhook(
            url,
            secret_token=config.telegram_webhook_secret.get_secret_value(),
            allowed_updates=["message"],
            drop_pending_updates=False,
        )
        info = await bot.get_webhook_info()
        print(f"Webhook: {info.url}; pending: {info.pending_update_count}")


if __name__ == "__main__":
    asyncio.run(main())
