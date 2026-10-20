# Mini Catalog

Каталог товаров с добавлением через Telegram. Администратор отправляет боту фото с названием в подписи, а товар появляется на сайте и в Mini App.

Next.js, FastAPI, aiogram, PostgreSQL и MinIO. Внешние запросы проходят через Nginx.

## Запуск

Нужны Docker с Linux-контейнерами и Docker Compose 2.30+.

```sh
cp .env.example .env
```

Заполните `.env`:

- `POSTGRES_PASSWORD` и пароль в `DATABASE_URL` должны совпадать. Для пароля в URL удобно использовать случайную hex-строку.
- `MINIO_ROOT_USER` должен совпадать с `MINIO_ACCESS_KEY`, а `MINIO_ROOT_PASSWORD` — с `MINIO_SECRET_KEY`.
- `BOT_API_SECRET` — случайный секрет от 32 символов.
- `TELEGRAM_WEBHOOK_SECRET` — отдельный секрет от 32 до 256 символов: латинские буквы, цифры, `_` и `-`.
- `BOT_TOKEN` — токен от BotFather; `TELEGRAM_ADMIN_ID` — числовой ID администратора.
- `MINI_APP_URL` — публичный HTTPS-адрес каталога.

Адреса `postgres` и `minio` в настройках оставьте как в примере: это имена сервисов внутри Docker.

```sh
docker compose up -d --build --scale backend=2 --scale bot=2
```

Каталог: [http://localhost:8080](http://localhost:8080). Миграции и создание хранилища выполняются автоматически. Первая сборка может занять несколько минут: MinIO собирается из исходников.

```sh
docker compose logs --tail=100 backend bot proxy
docker compose down
```

Обычная остановка сохраняет данные. `docker compose down -v` удаляет товары и фотографии.

## Telegram

Настройте HTTPS-домен или туннель на `http://localhost:8080`. Его адрес должен совпадать с `MINI_APP_URL`. Если меняли `.env` после запуска, повторите `docker compose up -d`.

Установите webhook, подставив свой домен:

```sh
docker compose exec --index 1 bot python -m bot.set_webhook https://catalog.example.com/telegram/webhook
```

Отправьте боту `/start` и нажмите «Каталог». Для добавления товара отправьте от аккаунта администратора отдельное фото до 5 МиБ с названием от 1 до 200 символов в подписи. После подтверждения нажмите «Обновить» в каталоге.

## Тесты

```sh
docker compose up -d postgres minio
docker compose --profile test run --build --rm tests
```

Тесты используют отдельную БД `catalog_test` и хранилище `catalog-test`. В GitHub Actions есть контейнерная проверка с двумя репликами backend и bot; Telegram API в ней заменён тестовым транспортом.
