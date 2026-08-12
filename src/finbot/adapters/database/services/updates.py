from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from finbot.adapters.database.models import ProcessedUpdate


async def is_update_processed(session: AsyncSession, update_id: int | None) -> bool:
    """Cheap replay preflight for handlers with expensive work before their atomic claim."""
    if update_id is None:
        return False
    return (
        await session.scalar(
            select(ProcessedUpdate.update_id).where(ProcessedUpdate.update_id == update_id)
        )
        is not None
    )


async def claim_update(session: AsyncSession, update_id: int | None) -> bool:
    """Atomically claim an update inside the caller's business transaction."""
    if update_id is None:
        return True
    result = await session.execute(
        insert(ProcessedUpdate)
        .values(update_id=update_id)
        .on_conflict_do_nothing(index_elements=[ProcessedUpdate.update_id])
        .returning(ProcessedUpdate.update_id)
    )
    return result.scalar_one_or_none() is not None
