from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from datetime import datetime
from typing import Protocol, Self
from uuid import UUID

from finbot.adapters.database.repositories.http_idempotency import (
    IdempotencyClaim,
    IdempotencyResult,
)
from finbot.adapters.database.repositories.security_values import (
    IdempotencyKeyDigest,
    RequestFingerprintDigest,
)
from finbot.adapters.http.auth.ports import AuthPersistence
from finbot.application.catalogs import (
    ArchiveAccountCommand,
    ArchiveCategoryCommand,
    CreateAccountCommand,
    CreateCategoryCommand,
    RestoreAccountCommand,
    RestoreCategoryCommand,
    SetDefaultAccountCommand,
    UpdateAccountCommand,
    UpdateCategoryCommand,
)
from finbot.application.dto import DraftSnapshot, MutationResult
from finbot.application.ports import DraftRepository
from finbot.application.revision_mutations import DraftPatchCommand, DraftPatchResult
from finbot.application.use_cases.bank_imports import BankImportUseCases
from finbot.application.use_cases.budgets import BudgetUseCases
from finbot.application.use_cases.exchange_rates import ExchangeRateUseCases
from finbot.application.use_cases.recurring import RecurringUseCases


class RevisionMutationCommands(Protocol):
    async def create_draft(self, owner_id: UUID) -> tuple[int, DraftSnapshot]: ...

    async def patch_draft(
        self,
        command: DraftPatchCommand,
    ) -> DraftPatchResult: ...

    async def confirm_draft(
        self,
        owner_id: UUID,
        draft_id: UUID,
        revision: int,
    ) -> IdempotencyResult: ...

    async def cancel_draft(
        self,
        owner_id: UUID,
        draft_id: UUID,
        revision: int,
    ) -> None: ...

    async def resolve_draft(
        self,
        owner_id: UUID,
        draft_id: UUID,
        revision: int,
        *,
        replace: bool,
    ) -> DraftSnapshot: ...

    async def repeat_transaction(
        self,
        owner_id: UUID,
        transaction_id: UUID,
        version: int,
    ) -> tuple[int, DraftSnapshot]: ...

    async def begin_transaction_edit(
        self,
        owner_id: UUID,
        transaction_id: UUID,
        version: int,
    ) -> tuple[int, DraftSnapshot]: ...

    async def delete_transaction(
        self,
        owner_id: UUID,
        transaction_id: UUID,
        version: int,
    ) -> IdempotencyResult: ...

    async def restore_transaction(
        self,
        owner_id: UUID,
        transaction_id: UUID,
        version: int,
    ) -> IdempotencyResult: ...


class CatalogMutationCommands(Protocol):
    async def create_account(self, command: CreateAccountCommand) -> MutationResult: ...

    async def update_account(self, command: UpdateAccountCommand) -> MutationResult: ...

    async def archive_account(self, command: ArchiveAccountCommand) -> MutationResult: ...

    async def restore_account(self, command: RestoreAccountCommand) -> MutationResult: ...

    async def set_default_account(self, command: SetDefaultAccountCommand) -> MutationResult: ...

    async def create_category(self, command: CreateCategoryCommand) -> MutationResult: ...

    async def update_category(self, command: UpdateCategoryCommand) -> MutationResult: ...

    async def archive_category(self, command: ArchiveCategoryCommand) -> MutationResult: ...

    async def restore_category(self, command: RestoreCategoryCommand) -> MutationResult: ...


class MutationIdempotency(Protocol):
    async def claim(
        self,
        owner_id: UUID,
        key: IdempotencyKeyDigest,
        fingerprint: RequestFingerprintDigest,
        *,
        operation: str,
        created_at: datetime,
        expires_at: datetime,
    ) -> IdempotencyClaim: ...

    async def complete(
        self,
        owner_id: UUID,
        claim: IdempotencyClaim,
        result: IdempotencyResult,
        *,
        completed_at: datetime,
    ) -> None: ...


class MutationUnitOfWork(Protocol):
    """One transaction ordered session -> owner -> idempotency -> domain."""

    auth: AuthPersistence
    budgets: BudgetUseCases
    bank_imports: BankImportUseCases
    recurring: RecurringUseCases
    catalogs: CatalogMutationCommands
    commands: RevisionMutationCommands
    drafts: DraftRepository
    exchange_rates: ExchangeRateUseCases
    idempotency: MutationIdempotency
    owner_timezone: str

    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exc_type: object,
        exc: object,
        traceback: object,
    ) -> bool | None: ...

    async def lock_owner(self, owner_id: UUID) -> None: ...


class MutationUnitOfWorkFactory(Protocol):
    def __call__(self) -> AbstractAsyncContextManager[MutationUnitOfWork]: ...
