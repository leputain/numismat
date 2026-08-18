from uuid import UUID

from finbot.application.undo import UndoActionResult, UndoRepository


class UndoLastAction:
    """Reverse one audited action without inferring intent from transaction rows."""

    __slots__ = ("_repository",)

    def __init__(self, repository: UndoRepository) -> None:
        self._repository = repository

    async def execute(self, owner_id: UUID) -> UndoActionResult | None:
        if not isinstance(owner_id, UUID):
            raise TypeError("Undo owner id must be a UUID")
        return await self._repository.undo_last(owner_id)
