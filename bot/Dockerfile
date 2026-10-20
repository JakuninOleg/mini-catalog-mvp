FROM python:3.12-slim AS runtime
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PATH="/app/.venv/bin:$PATH"
WORKDIR /app
COPY --from=ghcr.io/astral-sh/uv:0.11.30 /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project && useradd --uid 10001 --create-home app
COPY common ./common
COPY bot ./bot
USER 10001
CMD ["uvicorn", "bot.main:app", "--host", "0.0.0.0", "--port", "8001", "--no-access-log"]

FROM runtime AS ci
COPY tests/__init__.py tests/replica_bot.py tests/telegram_stub.py ./tests/
CMD ["uvicorn", "tests.replica_bot:app", "--host", "0.0.0.0", "--port", "8001", "--no-access-log"]

FROM runtime AS production
