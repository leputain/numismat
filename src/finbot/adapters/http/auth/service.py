from __future__ import annotations

import hmac
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from finbot.adapters.database.repositories.http_idempotency import (
    IdempotencyClaimStatus,
    IdempotencyResult,
    IdempotencyResultKind,
)
from finbot.adapters.http.auth.crypto import (
    HttpSecurityDigester,
    OpaqueAuthTokens,
    generate_auth_tokens,
    is_canonical_opaque_token,
)
from finbot.adapters.http.auth.ports import (
    AuthenticatedSession,
    AuthPersistence,
    AuthUnitOfWorkFactory,
    SessionCheckStatus,
)
from finbot.adapters.http.auth.telegram import (
    VerifiedTelegramAuth,
    verify_telegram_init_data,
)

SESSION_TTL = timedelta(hours=1)


class TelegramAuthReplayError(RuntimeError):
    pass


class AuthOwnerUnavailableError(RuntimeError):
    pass


class SessionInvalidError(RuntimeError):
    pass


class CsrfRejectedError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True, repr=False)
class LoginResult:
    authenticated: AuthenticatedSession = field(repr=False)
    tokens: OpaqueAuthTokens = field(repr=False)


def _utc_now() -> datetime:
    return datetime.now(UTC)


class SessionAuthenticator:
    """Reusable authentication gate over a caller-owned transaction boundary."""

    __slots__ = ("_digester",)

    def __init__(self, digester: HttpSecurityDigester) -> None:
        self._digester = digester

    async def authenticate_read(
        self,
        persistence: AuthPersistence,
        raw_session_token: str,
        *,
        now: datetime,
    ) -> AuthenticatedSession:
        if not is_canonical_opaque_token(raw_session_token):
            raise SessionInvalidError
        checked = await persistence.read_session(self._digester.session(raw_session_token), now=now)
        if checked.status is not SessionCheckStatus.ACTIVE or checked.authenticated is None:
            raise SessionInvalidError
        return checked.authenticated

    async def authenticate_mutation(
        self,
        persistence: AuthPersistence,
        raw_session_token: str,
        raw_csrf_cookie: str,
        raw_csrf_header: str,
        *,
        now: datetime,
    ) -> AuthenticatedSession:
        if (
            not is_canonical_opaque_token(raw_csrf_cookie)
            or not is_canonical_opaque_token(raw_csrf_header)
            or not hmac.compare_digest(raw_csrf_cookie, raw_csrf_header)
        ):
            raise CsrfRejectedError
        if not is_canonical_opaque_token(raw_session_token):
            raise SessionInvalidError
        checked = await persistence.lock_session_for_mutation(
            self._digester.session(raw_session_token),
            self._digester.csrf(raw_csrf_cookie),
            now=now,
        )
        if checked.status is SessionCheckStatus.CSRF_FAILED:
            raise CsrfRejectedError
        if checked.status is not SessionCheckStatus.ACTIVE or checked.authenticated is None:
            raise SessionInvalidError
        return checked.authenticated


class TelegramAuthService:
    __slots__ = (
        "_authenticator",
        "_bot_token",
        "_clock",
        "_digester",
        "_owner_telegram_user_id",
        "_token_factory",
        "_uow_factory",
    )

    def __init__(
        self,
        *,
        bot_token: str,
        owner_telegram_user_id: int,
        digester: HttpSecurityDigester,
        uow_factory: AuthUnitOfWorkFactory,
        clock: Callable[[], datetime] = _utc_now,
        token_factory: Callable[[], OpaqueAuthTokens] = generate_auth_tokens,
    ) -> None:
        self._bot_token = bot_token
        self._owner_telegram_user_id = owner_telegram_user_id
        self._digester = digester
        self._uow_factory = uow_factory
        self._clock = clock
        self._token_factory = token_factory
        self._authenticator = SessionAuthenticator(digester)

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("auth clock must be timezone-aware")
        return value

    def _verify(self, init_data: str, now: datetime) -> VerifiedTelegramAuth:
        return verify_telegram_init_data(
            init_data,
            bot_token=self._bot_token,
            owner_telegram_user_id=self._owner_telegram_user_id,
            now=now,
        )

    async def login(self, init_data: str) -> LoginResult:
        now = self._now()
        verified = self._verify(init_data, now)
        tokens = self._token_factory()
        if tokens.session_token == tokens.csrf_token:
            raise RuntimeError("opaque auth tokens must be independent")

        async with self._uow_factory() as persistence:
            owner = await persistence.find_owner(verified.telegram_user_id)
            if owner is None:
                raise AuthOwnerUnavailableError
            claim = await persistence.claim_auth_proof(
                owner.owner_id,
                self._digester.telegram_proof(verified.signed_proof_hash),
                self._digester.telegram_fingerprint(),
                created_at=now,
                expires_at=verified.proof_expires_at,
            )
            if claim.status is not IdempotencyClaimStatus.NEW:
                raise TelegramAuthReplayError
            expires_at = now + SESSION_TTL
            await persistence.create_session(
                owner.owner_id,
                self._digester.session(tokens.session_token),
                self._digester.csrf(tokens.csrf_token),
                created_at=now,
                expires_at=expires_at,
            )
            await persistence.complete_auth_proof(
                owner.owner_id,
                claim,
                IdempotencyResult(http_status=200, kind=IdempotencyResultKind.NONE),
                completed_at=now,
            )
        return LoginResult(
            authenticated=AuthenticatedSession(owner=owner, expires_at=expires_at),
            tokens=tokens,
        )

    async def read(self, raw_session_token: str) -> AuthenticatedSession:
        now = self._now()
        async with self._uow_factory() as persistence:
            return await self._authenticator.authenticate_read(
                persistence,
                raw_session_token,
                now=now,
            )

    async def logout(
        self,
        raw_session_token: str,
        raw_csrf_cookie: str,
        raw_csrf_header: str,
    ) -> None:
        now = self._now()
        if (
            not is_canonical_opaque_token(raw_csrf_cookie)
            or not is_canonical_opaque_token(raw_csrf_header)
            or not hmac.compare_digest(raw_csrf_cookie, raw_csrf_header)
        ):
            raise CsrfRejectedError
        if not is_canonical_opaque_token(raw_session_token):
            raise SessionInvalidError
        async with self._uow_factory() as persistence:
            checked = await persistence.revoke_session(
                self._digester.session(raw_session_token),
                self._digester.csrf(raw_csrf_cookie),
                now=now,
            )
            if checked.status is SessionCheckStatus.CSRF_FAILED:
                raise CsrfRejectedError
            if checked.status is not SessionCheckStatus.ACTIVE:
                raise SessionInvalidError
