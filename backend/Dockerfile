FROM python:3.12-slim AS runtime
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PATH="/app/.venv/bin:$PATH"
WORKDIR /app
COPY --from=ghcr.io/astral-sh/uv:0.11.30 /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project && useradd --uid 10001 --create-home app
COPY common ./common
COPY backend ./backend
COPY scripts ./scripts
USER 10001
CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]

FROM runtime AS tests
USER root
RUN uv sync --frozen --no-install-project
COPY .env.example ./
COPY bot ./bot
COPY tests ./tests
USER 10001
CMD ["sh", "-c", "python -m tests.prepare_db && alembic -c backend/alembic.ini upgrade head && pytest -q"]

FROM runtime AS production
