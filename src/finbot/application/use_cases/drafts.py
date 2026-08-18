from uuid import UUID

from finbot.application.dto import CreateDraftCommand, DraftRef, DraftSnapshot, UpdateDraftCommand
from finbot.application.errors import DraftRevisionConflictError, InvalidStateError
from finbot.application.ports import DraftRepository


def _validate_state(state: str) -> None:
    """Keep persistence limits out of presentation adapters."""

    if not state or len(state) > 30:
        raise InvalidStateError("Некорректное состояние черновика")


def _require_expected(current: DraftSnapshot | None, expected: DraftRef) -> DraftSnapshot:
    if current is None or current.ref != expected:
        raise DraftRevisionConflictError(
            current_revision=current.revision if current is not None else None
        )
    return current


class DraftUseCases:
    """Channel-neutral owner draft lifecycle.

    The adapter owns transaction boundaries.  Every mutation of an existing
    draft carries its UUID and revision so Telegram and HTTP clients share the
    same optimistic-concurrency contract.
    """

    def __init__(self, repository: DraftRepository) -> None:
        self._repository = repository

    async def get_active(self, owner_id: UUID) -> DraftSnapshot | None:
        return await self._repository.get_active(owner_id)

    async def create(self, command: CreateDraftCommand) -> DraftSnapshot:
        _validate_state(command.state)
        return await self._repository.create_if_absent(
            command.owner_id,
            command.state,
            command.payload,
        )

    async def update(self, command: UpdateDraftCommand) -> DraftSnapshot:
        _validate_state(command.state)
        return await self._repository.update(
            command.owner_id,
            command.expected,
            command.state,
            command.payload,
        )

    async def replace(self, command: UpdateDraftCommand) -> DraftSnapshot:
        _validate_state(command.state)
        return await self._repository.replace(
            command.owner_id,
            command.expected,
            command.state,
            command.payload,
        )

    async def resume(self, owner_id: UUID, expected: DraftRef) -> DraftSnapshot:
        return await self._repository.set_suspended(owner_id, expected, False)

    async def suspend(self, owner_id: UUID, expected: DraftRef) -> DraftSnapshot:
        """Pause one exact draft revision without exposing channel state."""

        return await self._repository.set_suspended(owner_id, expected, True)

    async def keep(self, owner_id: UUID, expected: DraftRef) -> DraftSnapshot:
        """Leave the active draft untouched after validating the client view."""

        current = await self.get_active(owner_id)
        return _require_expected(current, expected)

    async def cancel(self, owner_id: UUID, expected: DraftRef) -> None:
        await self._repository.delete(owner_id, expected)
