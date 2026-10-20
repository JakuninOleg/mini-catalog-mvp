"""Create the isolated test database before Alembic; never target the application database."""

import asyncio
import os

import asyncpg
from sqlalchemy.engine import make_url


async def main():
    url = make_url(os.environ["TEST_DATABASE_URL"])
    if url.database != "catalog_test":
        raise ValueError("Test bootstrap requires the exact catalog_test database name")
    admin_url = url.set(drivername="postgresql", database="postgres")
    connection = await asyncpg.connect(admin_url.render_as_string(hide_password=False), timeout=5)
    try:
        await connection.execute("CREATE DATABASE catalog_test")
    except asyncpg.DuplicateDatabaseError:
        pass
    finally:
        await connection.close()


if __name__ == "__main__":
    asyncio.run(main())
