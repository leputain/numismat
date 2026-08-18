from uuid import uuid7

import pytest
from fakes.repositories import InMemoryDraftRepository

from finbot.application.dto import CreateDraftCommand, DraftRef, UpdateDraftCommand
from finbot.application.errors import (
    ActiveDraftConflictError,
    DraftRevisionConflictError,
    InvalidStateError,
)
from finbot.application.use_cases.drafts import DraftUseCases


@pytest.mark.asyncio
async def test_create_get_update_and_cancel_draft() -> None:
    owner_id = uuid7()
    use_cases = DraftUseCases(InMemoryDraftRepository())

    created = await use_cases.create(CreateDraftCommand(owner_id, "amount", {"kind": "expense"}))
    assert await use_cases.get_active(owner_id) == created

    updated = await use_cases.update(
        UpdateDraftCommand(owner_id, created.ref, "review", {"kind": "expense"})
    )
    assert updated.draft_id == created.draft_id
    assert updated.revision == created.revision + 1

    await use_cases.cancel(owner_id, updated.ref)
    assert await use_cases.get_active(owner_id) is None


@pytest.mark.asyncio
async def test_create_rejects_a_second_active_draft() -> None:
    owner_id = uuid7()
    use_cases = DraftUseCases(InMemoryDraftRepository())
    await use_cases.create(CreateDraftCommand(owner_id, "amount", {}))

    with pytest.raises(ActiveDraftConflictError) as conflict:
        await use_cases.create(CreateDraftCommand(owner_id, "review", {}))

    assert conflict.value.current_revision == 1


@pytest.mark.asyncio
async def test_replace_invalidates_old_id_and_keep_does_not_mutate() -> None:
    owner_id = uuid7()
    use_cases = DraftUseCases(InMemoryDraftRepository())
    created = await use_cases.create(CreateDraftCommand(owner_id, "amount", {}))

    kept = await use_cases.keep(owner_id, created.ref)
    assert kept == created

    replacement = await use_cases.replace(
        UpdateDraftCommand(owner_id, created.ref, "review", {"kind": "income"})
    )
    assert replacement.draft_id != created.draft_id
    assert replacement.revision == 1
    with pytest.raises(DraftRevisionConflictError):
        await use_cases.keep(owner_id, created.ref)


@pytest.mark.asyncio
async def test_resume_is_revision_safe_and_idempotent_when_already_active() -> None:
    owner_id = uuid7()
    repository = InMemoryDraftRepository()
    use_cases = DraftUseCases(repository)
    created = await use_cases.create(CreateDraftCommand(owner_id, "review", {}))
    suspended = await repository.set_suspended(owner_id, created.ref, True)

    resumed = await use_cases.resume(owner_id, suspended.ref)
    assert resumed.suspended is False
    assert resumed.revision == suspended.revision + 1

    unchanged = await use_cases.resume(owner_id, resumed.ref)
    assert unchanged == resumed


@pytest.mark.asyncio
async def test_suspend_is_revision_safe_and_idempotent_when_already_suspended() -> None:
    owner_id = uuid7()
    use_cases = DraftUseCases(InMemoryDraftRepository())
    created = await use_cases.create(CreateDraftCommand(owner_id, "review", {}))

    suspended = await use_cases.suspend(owner_id, created.ref)
    assert suspended.suspended is True
    assert suspended.revision == created.revision + 1

    unchanged = await use_cases.suspend(owner_id, suspended.ref)
    assert unchanged == suspended


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["update", "replace", "resume", "suspend", "keep", "cancel"])
async def test_existing_draft_operations_reject_stale_ref(operation: str) -> None:
    owner_id = uuid7()
    use_cases = DraftUseCases(InMemoryDraftRepository())
    created = await use_cases.create(CreateDraftCommand(owner_id, "amount", {}))
    stale = DraftRef(uuid7(), created.revision)

    with pytest.raises(DraftRevisionConflictError):
        if operation == "update":
            await use_cases.update(UpdateDraftCommand(owner_id, stale, "review", {}))
        elif operation == "replace":
            await use_cases.replace(UpdateDraftCommand(owner_id, stale, "review", {}))
        elif operation == "resume":
            await use_cases.resume(owner_id, stale)
        elif operation == "suspend":
            await use_cases.suspend(owner_id, stale)
        elif operation == "keep":
            await use_cases.keep(owner_id, stale)
        else:
            await use_cases.cancel(owner_id, stale)


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["", "x" * 31])
async def test_state_is_validated_before_repository_mutation(state: str) -> None:
    owner_id = uuid7()
    use_cases = DraftUseCases(InMemoryDraftRepository())

    with pytest.raises(InvalidStateError):
        await use_cases.create(CreateDraftCommand(owner_id, state, {}))
    assert await use_cases.get_active(owner_id) is None
