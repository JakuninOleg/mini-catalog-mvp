import asyncio
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend import main


def test_create_catalog_image_and_repeat(api, payload, photo):
    first = api.post("/products", json=payload)
    assert first.status_code == 201, first.text
    assert first.json()["created"] is True
    again = api.post("/products", json=payload)
    assert again.status_code == 200
    assert again.json()["created"] is False
    assert again.json()["product"] == first.json()["product"]
    listing = api.get("/products")
    assert listing.headers["cache-control"] == "no-store"
    assert [p["title"] for p in listing.json()] == ["Белая чашка"]
    assert api.get("/products/1/image").content == photo
    assert api.get("/products/999/image").status_code == 404
    assert api.get("/health").status_code == 200


def test_unauthorized(api, payload):
    assert api.post("/products", json=payload, headers={"X-Bot-Secret": "wrong"}).status_code == 401
    assert api.get("/products").json() == []


@pytest.mark.parametrize(
    "changes",
    [
        {"title": " "},
        {"title": "x" * 201},
        {"title": "a\nb"},
        {"image_base64": "not base64"},
        {"image_base64": "aGVsbG8="},
        {"telegram_update_id": -1},
        {"telegram_update_id": True},
        {"extra": "forbidden"},
    ],
)
def test_invalid(api, payload, changes):
    assert api.post("/products", json=payload | changes).status_code == 422
    assert api.get("/products").json() == []


def test_concurrent_and_conflicting_payload(api, payload):
    with ThreadPoolExecutor(max_workers=10) as workers:
        results = list(workers.map(lambda _: api.post("/products", json=payload), range(10)))
    assert sorted(r.status_code for r in results) == [200] * 9 + [201]
    assert sum(r.json()["created"] for r in results) == 1
    assert len(api.get("/products").json()) == 1
    conflict = api.post("/products", json=payload | {"title": "Другое имя"})
    assert conflict.status_code == 409


def test_minio_failure_rolls_back_then_retry(api, payload, monkeypatch):
    original = main.app.state.storage.put_object

    def fail(*args, **kwargs):
        raise OSError("Storage disconnected")

    monkeypatch.setattr(main.app.state.storage, "put_object", fail)
    assert api.post("/products", json=payload).status_code == 503
    assert api.get("/products").json() == []
    monkeypatch.setattr(main.app.state.storage, "put_object", original)
    assert api.post("/products", json=payload).status_code == 201


def test_write_succeeded_but_storage_reply_lost(api, payload, monkeypatch):
    original = main.app.state.storage.put_object

    def uncertain(*args, **kwargs):
        original(*args, **kwargs)
        raise OSError("S3 reply lost after write")

    monkeypatch.setattr(main.app.state.storage, "put_object", uncertain)
    assert api.post("/products", json=payload).status_code == 503
    assert api.get("/products").json() == []
    monkeypatch.setattr(main.app.state.storage, "put_object", original)
    assert api.post("/products", json=payload).status_code == 201
    assert len(api.get("/products").json()) == 1


def test_concurrent_retry_after_uncertain_upload_keeps_image(api, payload, photo, monkeypatch):
    uploaded, release = Event(), Event()
    original = main.app.state.storage.put_object

    def uncertain_first_put(*args, **kwargs):
        result = original(*args, **kwargs)
        if not uploaded.is_set():
            uploaded.set()
            assert release.wait(10), "Test failed to release the first transaction"
            raise OSError("S3 response lost while another request waits")
        return result

    async def wait_for_competing_insert():
        async with main.app.state.sessions() as db:
            for _ in range(100):
                waiting = await db.scalar(
                    text("""
                    SELECT EXISTS (SELECT 1 FROM pg_stat_activity
                      WHERE datname = current_database() AND wait_event_type = 'Lock'
                        AND query LIKE 'INSERT INTO products%')
                """)
                )
                if waiting:
                    return
                await db.rollback()
                await asyncio.sleep(0.025)
        raise AssertionError("Second request did not wait on PostgreSQL's unique index")

    monkeypatch.setattr(main.app.state.storage, "put_object", uncertain_first_put)
    with ThreadPoolExecutor(max_workers=2) as workers:
        first = workers.submit(api.post, "/products", json=payload)
        try:
            assert uploaded.wait(5)
            second = workers.submit(api.post, "/products", json=payload)
            api.portal.call(wait_for_competing_insert)
        finally:
            release.set()
        assert first.result().status_code == 503
        result = second.result()
        assert result.status_code == 201
    assert api.post("/products", json=payload).status_code == 200
    assert len(api.get("/products").json()) == 1
    assert api.get(f"/products/{result.json()['product']['id']}/image").content == photo


def test_database_unavailable_returns_503(api, payload):
    original = main.app.state.sessions
    engine = create_async_engine(
        "postgresql+asyncpg://catalog@127.0.0.1:1/catalog_test", connect_args={"timeout": 1}
    )
    main.app.state.sessions = async_sessionmaker(engine)
    try:
        assert api.get("/products").status_code == 503
        assert api.post("/products", json=payload).status_code == 503
    finally:
        main.app.state.sessions = original
        api.portal.call(engine.dispose)


def test_commit_failure_after_upload_then_retry(api, payload):
    # A deferred constraint trigger fails COMMIT, after the real S3 PUT completed.
    async def install():
        async with main.app.state.sessions() as db:
            await db.execute(
                text("""
                CREATE OR REPLACE FUNCTION fail_test_commit() RETURNS trigger AS $$
                BEGIN RAISE EXCEPTION 'injected commit failure'; END;
                $$ LANGUAGE plpgsql
            """)
            )
            await db.execute(
                text("""
                CREATE CONSTRAINT TRIGGER test_commit_failure AFTER INSERT ON products
                DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION fail_test_commit()
            """)
            )
            await db.commit()

    async def remove():
        async with main.app.state.sessions() as db:
            await db.execute(text("DROP TRIGGER IF EXISTS test_commit_failure ON products"))
            await db.execute(text("DROP FUNCTION IF EXISTS fail_test_commit()"))
            await db.commit()

    api.portal.call(install)
    try:
        assert api.post("/products", json=payload).status_code == 503
        assert api.get("/products").json() == []
    finally:
        api.portal.call(remove)
    assert api.post("/products", json=payload).status_code == 201


def test_cursor_pagination(api, payload):
    for update_id in range(3):
        assert (
            api.post("/products", json=payload | {"telegram_update_id": update_id}).status_code
            == 201
        )
    page = api.get("/products?limit=2").json()
    next_page = api.get(f"/products?before_id={page[-1]['id']}&limit=2").json()
    assert len(page) == 2 and len(next_page) == 1
    assert set(p["id"] for p in page).isdisjoint(p["id"] for p in next_page)
