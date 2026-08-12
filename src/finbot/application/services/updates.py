from typing import Protocol


class UpdateDeduplicator(Protocol):
    """Application contract for atomically claiming an inbound update."""

    async def claim(self, update_id: int | None) -> bool: ...
