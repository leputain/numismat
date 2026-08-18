from uuid import UUID

from finbot.application.undo import UndoActionResult


class InMemoryUndoRepository:
    """Deterministic undo port fake; it never infers a result from other state."""

    def __init__(self, result: UndoActionResult | None) -> None:
        self.result = result
        self.calls: list[UUID] = []

    async def undo_last(self, owner_id: UUID) -> UndoActionResult | None:
        self.calls.append(owner_id)
        return self.result
