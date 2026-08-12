import os
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine


@pytest.fixture(scope="session", autouse=True)
async def require_isolated_test_database() -> AsyncIterator[None]:
    """Fail before any fixture cleanup unless both configured and connected DBs are `_test`."""
    configured = os.environ.get("TEST_DATABASE_URL", "")
    if not configured:
        pytest.fail("TEST_DATABASE_URL is required for integration tests")
    database = make_url(configured).database or ""
    if not database.endswith("_test"):
        pytest.fail("TEST_DATABASE_URL database name must end in _test")

    engine = create_async_engine(configured)
    try:
        async with engine.connect() as connection:
            current = str(await connection.scalar(text("SELECT current_database()")))
    finally:
        await engine.dispose()
    if current != database or not current.endswith("_test"):
        pytest.fail("connected database must match TEST_DATABASE_URL and end in _test")
    yield
