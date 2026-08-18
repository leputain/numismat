import os

import httpx2
import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from finbot.adapters.database.repositories.readiness import SqlAlchemyReadinessProbe
from finbot.adapters.http.app import create_app

DATABASE_URL = os.environ["TEST_DATABASE_URL"]


@pytest.mark.asyncio
async def test_http_readiness_checks_database_and_exact_migration_head() -> None:
    engine = create_async_engine(DATABASE_URL)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    current_head = ScriptDirectory.from_config(Config("alembic.ini")).get_current_head()
    assert current_head is not None
    app = create_app(
        readiness_probe=SqlAlchemyReadinessProbe(sessions, expected_revision=current_head)
    )
    transport = httpx2.ASGITransport(app=app)
    try:
        async with httpx2.AsyncClient(
            transport=transport,
            base_url="https://numismat.invalid",
        ) as client:
            healthy = await client.get("/health/ready")
            async with sessions.begin() as session:
                await session.execute(
                    text("UPDATE alembic_version SET version_num = 'synthetic_wrong_revision'")
                )
            mismatched = await client.get("/health/ready")
            async with sessions.begin() as session:
                await session.execute(
                    text("UPDATE alembic_version SET version_num = :revision"),
                    {"revision": current_head},
                )
    finally:
        await engine.dispose()

    assert healthy.status_code == 200
    assert healthy.json() == {"status": "ok"}
    assert mismatched.status_code == 503
    assert mismatched.json()["error"]["code"] == "readiness_failed"
