from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from finbot.adapters.database.models import User, WebSession
from finbot.adapters.database.repositories.http_idempotency import (
    IdempotencyClaim,
    IdempotencyResult,
    SqlAlchemyHttpIdempotencyRepository,
)
from finbot.adapters.database.repositories.security_values import (
    CsrfTokenDigest,
    IdempotencyKeyDigest,
    RequestFingerprintDigest,
    SessionTokenDigest,
)
from finbot.adapters.database.repositories.web_sessions import (
    SqlAlchemyWebSessionRepository,
)
from finbot.adapters.http.auth.ports import (
    AuthenticatedSession,
    AuthOwner,
    AuthPersistence,
    SessionCheck,
    SessionCheckStatus,
)

AUTH_OPERATION = "auth.telegram"


def _owner(user: User) -> AuthOwner:
    return AuthOwner(
        owner_id=user.id,
        locale=user.locale,
        timezone=user.timezone,
        base_currency=user.base_currency,
        telegram_user_id=user.telegram_user_id,
    )


class SqlAlchemyHttpSessionAuthenticator:
    """Session checks that retain PostgreSQL locks in the caller-owned transaction."""

    __slots__ = ("_session",)

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def _load(
        self,
        session_token: SessionTokenDigest,
        *,
        now: datetime,
        mutation_lock: bool,
    ) -> tuple[WebSession, User] | None:
        statement = (
            select(WebSession, User)
            .join(User, User.id == WebSession.user_id)
            .where(
                WebSession.session_token_hash == session_token.database_value(),
                WebSession.revoked_at.is_(None),
                WebSession.expires_at > now,
            )
        )
        if mutation_lock:
            statement = statement.with_for_update(read=True, of=WebSession)
        row = (await self._session.execute(statement)).one_or_none()
        if row is None:
            return None
        return row._t

    @staticmethod
    def _active(web_session: WebSession, user: User) -> SessionCheck:
        return SessionCheck(
            status=SessionCheckStatus.ACTIVE,
            authenticated=AuthenticatedSession(
                owner=_owner(user),
                expires_at=web_session.expires_at,
            ),
        )

    async def read(
        self,
        session_token: SessionTokenDigest,
        *,
        now: datetime,
    ) -> SessionCheck:
        row = await self._load(session_token, now=now, mutation_lock=False)
        if row is None:
            return SessionCheck(status=SessionCheckStatus.INVALID)
        return self._active(*row)

    async def lock_for_login(
        self,
        session_token: SessionTokenDigest,
        *,
        now: datetime,
    ) -> SessionCheck:
        row = (
            await self._session.execute(
                select(WebSession, User)
                .join(User, User.id == WebSession.user_id)
                .where(
                    WebSession.session_token_hash == session_token.database_value(),
                    WebSession.revoked_at.is_(None),
                    WebSession.expires_at > now,
                )
                .with_for_update(of=WebSession)
            )
        ).one_or_none()
        if row is None:
            return SessionCheck(status=SessionCheckStatus.INVALID)
        return self._active(*row._t)

    async def revoke_for_login(
        self,
        session_token: SessionTokenDigest,
        *,
        now: datetime,
    ) -> None:
        web_session = await self._session.scalar(
            select(WebSession)
            .where(
                WebSession.session_token_hash == session_token.database_value(),
                WebSession.revoked_at.is_(None),
                WebSession.expires_at > now,
            )
            .with_for_update()
        )
        if web_session is not None:
            web_session.revoked_at = now
            await self._session.flush()

    async def lock_for_mutation(
        self,
        session_token: SessionTokenDigest,
        csrf_token: CsrfTokenDigest,
        *,
        now: datetime,
    ) -> SessionCheck:
        row = await self._load(session_token, now=now, mutation_lock=True)
        if row is None:
            return SessionCheck(status=SessionCheckStatus.INVALID)
        web_session, user = row
        if not csrf_token.matches(web_session.csrf_token_hash):
            return SessionCheck(status=SessionCheckStatus.CSRF_FAILED)
        return self._active(web_session, user)

    async def revoke(
        self,
        session_token: SessionTokenDigest,
        csrf_token: CsrfTokenDigest,
        *,
        now: datetime,
    ) -> SessionCheck:
        row = (
            await self._session.execute(
                select(WebSession, User)
                .join(User, User.id == WebSession.user_id)
                .where(
                    WebSession.session_token_hash == session_token.database_value(),
                    WebSession.revoked_at.is_(None),
                    WebSession.expires_at > now,
                )
                .with_for_update(of=WebSession)
            )
        ).one_or_none()
        if row is None:
            return SessionCheck(status=SessionCheckStatus.INVALID)
        web_session, user = row._t
        if not csrf_token.matches(web_session.csrf_token_hash):
            return SessionCheck(status=SessionCheckStatus.CSRF_FAILED)
        web_session.revoked_at = now
        await self._session.flush()
        return self._active(web_session, user)


class SqlAlchemyAuthPersistence:
    __slots__ = ("_idempotency", "_session", "_session_authenticator", "_web_sessions")

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._web_sessions = SqlAlchemyWebSessionRepository(session)
        self._idempotency = SqlAlchemyHttpIdempotencyRepository(session)
        self._session_authenticator = SqlAlchemyHttpSessionAuthenticator(session)

    async def find_owner(self, telegram_user_id: int) -> AuthOwner | None:
        user = await self._session.scalar(
            select(User).where(User.telegram_user_id == telegram_user_id)
        )
        return _owner(user) if user is not None else None

    async def create_session(
        self,
        owner_id: UUID,
        session_token: SessionTokenDigest,
        csrf_token: CsrfTokenDigest,
        *,
        created_at: datetime,
        expires_at: datetime,
    ) -> None:
        await self._web_sessions.create(
            owner_id,
            session_token,
            csrf_token,
            created_at=created_at,
            expires_at=expires_at,
        )

    async def claim_auth_proof(
        self,
        owner_id: UUID,
        key: IdempotencyKeyDigest,
        fingerprint: RequestFingerprintDigest,
        *,
        created_at: datetime,
        expires_at: datetime,
    ) -> IdempotencyClaim:
        return await self._idempotency.claim(
            owner_id,
            key,
            fingerprint,
            operation=AUTH_OPERATION,
            created_at=created_at,
            expires_at=expires_at,
        )

    async def complete_auth_proof(
        self,
        owner_id: UUID,
        claim: IdempotencyClaim,
        result: IdempotencyResult,
        *,
        completed_at: datetime,
    ) -> None:
        await self._idempotency.complete(
            owner_id,
            claim,
            result,
            completed_at=completed_at,
        )

    async def read_session(
        self,
        session_token: SessionTokenDigest,
        *,
        now: datetime,
    ) -> SessionCheck:
        return await self._session_authenticator.read(session_token, now=now)

    async def lock_session_for_login(
        self,
        session_token: SessionTokenDigest,
        *,
        now: datetime,
    ) -> SessionCheck:
        return await self._session_authenticator.lock_for_login(session_token, now=now)

    async def revoke_session_for_login(
        self,
        session_token: SessionTokenDigest,
        *,
        now: datetime,
    ) -> None:
        await self._session_authenticator.revoke_for_login(session_token, now=now)

    async def lock_session_for_mutation(
        self,
        session_token: SessionTokenDigest,
        csrf_token: CsrfTokenDigest,
        *,
        now: datetime,
    ) -> SessionCheck:
        return await self._session_authenticator.lock_for_mutation(
            session_token,
            csrf_token,
            now=now,
        )

    async def revoke_session(
        self,
        session_token: SessionTokenDigest,
        csrf_token: CsrfTokenDigest,
        *,
        now: datetime,
    ) -> SessionCheck:
        return await self._session_authenticator.revoke(
            session_token,
            csrf_token,
            now=now,
        )


class SqlAlchemyAuthUnitOfWorkFactory:
    __slots__ = ("_sessions",)

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    @asynccontextmanager
    async def __call__(self) -> AsyncIterator[AuthPersistence]:
        async with self._sessions.begin() as session:
            yield SqlAlchemyAuthPersistence(session)


class SqlAlchemyAuthenticatedMutationUnitOfWork:
    """Extensible UoW that retains the session-row lock through outer commit."""

    __slots__ = ("_context", "_sessions", "persistence", "session")

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions
        self._context = sessions.begin()
        self.session: AsyncSession
        self.persistence: AuthPersistence

    async def __aenter__(self) -> SqlAlchemyAuthenticatedMutationUnitOfWork:
        self.session = await self._context.__aenter__()
        self.persistence = SqlAlchemyAuthPersistence(self.session)
        return self

    async def __aexit__(
        self,
        exc_type: object,
        exc: object,
        traceback: object,
    ) -> bool | None:
        await self._context.__aexit__(exc_type, exc, traceback)
        return None


class SqlAlchemyAuthenticatedMutationUnitOfWorkFactory:
    __slots__ = ("_sessions",)

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    def __call__(self) -> SqlAlchemyAuthenticatedMutationUnitOfWork:
        return SqlAlchemyAuthenticatedMutationUnitOfWork(self._sessions)
