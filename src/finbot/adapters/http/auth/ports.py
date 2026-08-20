from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Protocol, Self
from uuid import UUID

from finbot.adapters.database.repositories.http_idempotency import (
    IdempotencyClaim,
    IdempotencyResult,
)
from finbot.adapters.database.repositories.security_values import (
    CsrfTokenDigest,
    IdempotencyKeyDigest,
    RequestFingerprintDigest,
    SessionTokenDigest,
)


@dataclass(frozen=True, slots=True, repr=False)
class AuthOwner:
    owner_id: UUID = field(repr=False)
    locale: str
    timezone: str
    base_currency: str
    telegram_user_id: int = field(default=0, repr=False)


@dataclass(frozen=True, slots=True, repr=False)
class AuthenticatedSession:
    owner: AuthOwner = field(repr=False)
    expires_at: datetime


class SessionCheckStatus(StrEnum):
    ACTIVE = "active"
    INVALID = "invalid"
    CSRF_FAILED = "csrf_failed"


@dataclass(frozen=True, slots=True, repr=False)
class SessionCheck:
    status: SessionCheckStatus
    authenticated: AuthenticatedSession | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if (self.status is SessionCheckStatus.ACTIVE) != (self.authenticated is not None):
            raise ValueError("only an active session check carries authentication")


class AuthPersistence(Protocol):
    async def find_owner(self, telegram_user_id: int) -> AuthOwner | None: ...

    async def create_session(
        self,
        owner_id: UUID,
        session_token: SessionTokenDigest,
        csrf_token: CsrfTokenDigest,
        *,
        created_at: datetime,
        expires_at: datetime,
    ) -> None: ...

    async def claim_auth_proof(
        self,
        owner_id: UUID,
        key: IdempotencyKeyDigest,
        fingerprint: RequestFingerprintDigest,
        *,
        created_at: datetime,
        expires_at: datetime,
    ) -> IdempotencyClaim: ...

    async def complete_auth_proof(
        self,
        owner_id: UUID,
        claim: IdempotencyClaim,
        result: IdempotencyResult,
        *,
        completed_at: datetime,
    ) -> None: ...

    async def read_session(
        self,
        session_token: SessionTokenDigest,
        *,
        now: datetime,
    ) -> SessionCheck: ...

    async def lock_session_for_login(
        self,
        session_token: SessionTokenDigest,
        *,
        now: datetime,
    ) -> SessionCheck: ...

    async def revoke_session_for_login(
        self,
        session_token: SessionTokenDigest,
        *,
        now: datetime,
    ) -> None: ...

    async def lock_session_for_mutation(
        self,
        session_token: SessionTokenDigest,
        csrf_token: CsrfTokenDigest,
        *,
        now: datetime,
    ) -> SessionCheck: ...

    async def revoke_session(
        self,
        session_token: SessionTokenDigest,
        csrf_token: CsrfTokenDigest,
        *,
        now: datetime,
    ) -> SessionCheck: ...


class AuthUnitOfWorkFactory(Protocol):
    def __call__(self) -> AbstractAsyncContextManager[AuthPersistence]: ...


class AuthenticatedMutationUnitOfWork(Protocol):
    """Caller-owned transaction held from session lock through business commit."""

    persistence: AuthPersistence

    async def __aenter__(self) -> Self: ...

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> bool | None: ...


class AuthenticatedMutationUnitOfWorkFactory(Protocol):
    def __call__(self) -> AuthenticatedMutationUnitOfWork: ...
