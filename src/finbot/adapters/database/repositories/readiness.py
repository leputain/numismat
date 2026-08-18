from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


@dataclass(frozen=True, slots=True)
class SqlAlchemyReadinessProbe:
    sessions: async_sessionmaker[AsyncSession]
    expected_revision: str

    def __post_init__(self) -> None:
        if not self.expected_revision:
            raise ValueError("expected Alembic revision is required")

    async def check(self) -> None:
        async with self.sessions() as session:
            await session.execute(text("SELECT 1"))
            current = await session.scalar(text("SELECT version_num FROM alembic_version"))
        if type(current) is not str or current != self.expected_revision:
            raise RuntimeError("database migration revision mismatch")
