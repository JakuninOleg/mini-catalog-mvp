import hashlib
import io
import logging
import secrets
from contextlib import asynccontextmanager
from datetime import datetime
from functools import lru_cache
from typing import Annotated

import urllib3
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Response
from fastapi.responses import JSONResponse
from minio import Minio
from minio.error import S3Error
from pydantic import BaseModel, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import BigInteger, DateTime, String, func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from starlette.concurrency import run_in_threadpool

from common.contracts import (
    MAX_IMAGE_BYTES,
    MAX_JSON_BYTES,
    BodyLimitMiddleware,
    ProductCreate,
    decode_image,
)

logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    database_url: str
    bot_api_secret: SecretStr = Field(min_length=32)
    minio_endpoint: str = "minio:9000"
    minio_access_key: str
    minio_secret_key: SecretStr
    minio_bucket: str = "catalog"
    minio_secure: bool = False


@lru_cache
def settings() -> Settings:
    return Settings()


class Base(DeclarativeBase):
    pass


class Product(Base):
    __tablename__ = "products"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    title: Mapped[str] = mapped_column(String(200))
    image_key: Mapped[str] = mapped_column(String(160))
    telegram_update_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    payload_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ProductOut(BaseModel):
    id: int
    title: str
    image_url: str
    created_at: datetime


class CreateResult(BaseModel):
    product: ProductOut
    created: bool


def public_product(product: Product) -> ProductOut:
    return ProductOut(
        id=product.id,
        title=product.title,
        image_url=f"/api/products/{product.id}/image",
        created_at=product.created_at,
    )


def storage_client() -> Minio:
    config = settings()
    return Minio(
        config.minio_endpoint,
        access_key=config.minio_access_key,
        secret_key=config.minio_secret_key.get_secret_value(),
        secure=config.minio_secure,
        http_client=urllib3.PoolManager(timeout=urllib3.Timeout(connect=3, read=8), retries=0),
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    engine = create_async_engine(
        settings().database_url,
        pool_pre_ping=True,
        connect_args={
            "timeout": 5,
            "command_timeout": 25,
            "server_settings": {"lock_timeout": "15000"},
        },
    )
    app.state.sessions = async_sessionmaker(engine, expire_on_commit=False)
    app.state.storage = storage_client()
    yield
    await engine.dispose()


app = FastAPI(title="Mini Catalog", lifespan=lifespan, docs_url=None, redoc_url=None)
app.add_middleware(BodyLimitMiddleware, limit=MAX_JSON_BYTES)


async def session():
    async with app.state.sessions() as db:
        yield db


Db = Annotated[AsyncSession, Depends(session)]


def authorize(x_bot_secret: Annotated[str | None, Header()] = None):
    if not x_bot_secret or not secrets.compare_digest(
        x_bot_secret.encode(), settings().bot_api_secret.get_secret_value().encode()
    ):
        raise HTTPException(401, "Unauthorized")


@app.exception_handler(SQLAlchemyError)
@app.exception_handler(OSError)
@app.exception_handler(TimeoutError)
async def database_error(_request, exc):
    logger.error("Database unavailable: %s", type(exc).__name__)
    return JSONResponse(status_code=503, content={"detail": "Database unavailable"})


@app.get("/health")
async def health(db: Db):
    await db.execute(text("SELECT 1"))
    try:
        exists = await run_in_threadpool(app.state.storage.bucket_exists, settings().minio_bucket)
    except (S3Error, urllib3.exceptions.HTTPError, OSError) as exc:
        raise HTTPException(503, "Storage unavailable") from exc
    if not exists:
        raise HTTPException(503, "Storage not initialized")
    return {"status": "ok"}


@app.get("/products", response_model=list[ProductOut])
async def products(
    db: Db,
    response: Response,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
    before_id: Annotated[int | None, Query(ge=1)] = None,
):
    query = select(Product).order_by(Product.id.desc()).limit(limit)
    if before_id is not None:
        query = query.where(Product.id < before_id)
    rows = (await db.scalars(query)).all()
    response.headers["Cache-Control"] = "no-store"
    return [public_product(row) for row in rows]


@app.post("/products", response_model=CreateResult, dependencies=[Depends(authorize)])
async def create_product(payload: ProductCreate, db: Db, response: Response):
    try:
        data, content_type = await run_in_threadpool(decode_image, payload.image_base64)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    image_hash = hashlib.sha256(data).hexdigest()
    payload_hash = hashlib.sha256((payload.title + "\0" + image_hash).encode()).hexdigest()
    key = f"products/{payload.telegram_update_id}/{image_hash}"
    async with db.begin():
        statement = (
            insert(Product)
            .values(
                title=payload.title,
                image_key=key,
                telegram_update_id=payload.telegram_update_id,
                payload_hash=payload_hash,
            )
            .on_conflict_do_nothing(index_elements=[Product.telegram_update_id])
            .returning(Product)
        )
        product = (await db.scalars(statement)).one_or_none()
        created = product is not None
        if created:
            try:
                # No delete on failure: a retry may already be using this immutable object key.
                await run_in_threadpool(
                    app.state.storage.put_object,
                    settings().minio_bucket,
                    key,
                    io.BytesIO(data),
                    len(data),
                    content_type=content_type,
                )
            except (S3Error, urllib3.exceptions.HTTPError, OSError) as exc:
                raise HTTPException(503, "Storage unavailable; retry the same update") from exc
        else:
            product = (
                await db.scalars(
                    select(Product).where(Product.telegram_update_id == payload.telegram_update_id)
                )
            ).one()
            if product.payload_hash != payload_hash:
                raise HTTPException(409, "Update ID already used with different content")
    response.status_code = 201 if created else 200
    return CreateResult(product=public_product(product), created=created)


@app.get("/products/{product_id}/image")
async def product_image(product_id: int, db: Db):
    product = await db.get(Product, product_id)
    if product is None:
        raise HTTPException(404, "Product not found")

    def read():
        obj = app.state.storage.get_object(settings().minio_bucket, product.image_key)
        try:
            data = obj.read(MAX_IMAGE_BYTES + 1)
            if len(data) > MAX_IMAGE_BYTES:
                raise HTTPException(502, "Invalid stored image")
            return data, obj.headers.get("Content-Type", "application/octet-stream")
        finally:
            obj.close()
            obj.release_conn()

    try:
        data, content_type = await run_in_threadpool(read)
    except (S3Error, urllib3.exceptions.HTTPError, OSError) as exc:
        raise HTTPException(503, "Image unavailable") from exc
    return Response(
        data,
        media_type=content_type,
        headers={"Cache-Control": "public, max-age=86400", "X-Content-Type-Options": "nosniff"},
    )
