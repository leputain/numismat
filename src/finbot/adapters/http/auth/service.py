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
    MAX_ALLOWED_TELEGRAM_USERS,
    MAX_TELEGRAM_USER_ID,
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


class SessionBindingMismatchError(RuntimeError):
    pass


class CsrfRejectedError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True, repr=False)
class LoginResult:
    authenticated: AuthenticatedSession = field(repr=False)
    session_binding: str = field(repr=False)
    tokens: OpaqueAuthTokens | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True, repr=False)
class SessionCredentials:
    session_token: str = field(repr=False)
    session_binding: str = field(repr=False)


def _utc_now() -> datetime:
    return datetime.now(UTC)


class SessionAuthenticator:
    """Reusable authentication gate over a caller-owned transaction boundary."""

    __slots__ = ("_allowed_telegram_user_ids", "_digester")

    def __init__(
        self,
        digester: HttpSecurityDigester,
        allowed_telegram_user_ids: frozenset[int] | None = None,
    ) -> None:
        self._digester = digester
        self._allowed_telegram_user_ids = (
            _validate_allowlist(allowed_telegram_user_ids)
            if allowed_telegram_user_ids is not None
            else None
        )

    def ensure_allowed(self, authenticated: AuthenticatedSession) -> None:
        if (
            self._allowed_telegram_user_ids is not None
            and authenticated.owner.telegram_user_id not in self._allowed_telegram_user_ids
        ):
            raise SessionInvalidError

    def ensure_bound(self, raw_session_token: str, raw_session_binding: str) -> None:
        if not is_canonical_opaque_token(raw_session_token) or not is_canonical_opaque_token(
            raw_session_binding
        ):
            raise SessionInvalidError
        if not hmac.compare_digest(
            self._digester.session_binding(raw_session_token),
            raw_session_binding,
        ):
            raise SessionBindingMismatchError

    async def authenticate_read(
        self,
        persistence: AuthPersistence,
        credentials: SessionCredentials,
        *,
        now: datetime,
    ) -> AuthenticatedSession:
        self.ensure_bound(credentials.session_token, credentials.session_binding)
        checked = await persistence.read_session(
            self._digester.session(credentials.session_token),
            now=now,
        )
        if checked.status is not SessionCheckStatus.ACTIVE or checked.authenticated is None:
            raise SessionInvalidError
        self.ensure_allowed(checked.authenticated)
        return checked.authenticated

    async def authenticate_mutation(
        self,
        persistence: AuthPersistence,
        raw_session_token: str,
        raw_session_binding: str,
        raw_csrf_cookie: str,
        raw_csrf_header: str,
        *,
        now: datetime,
    ) -> AuthenticatedSession:
        self.ensure_bound(raw_session_token, raw_session_binding)
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
        self.ensure_allowed(checked.authenticated)
        return checked.authenticated


def _validate_allowlist(value: frozenset[int]) -> frozenset[int]:
    if type(value) is not frozenset or not 1 <= len(value) <= MAX_ALLOWED_TELEGRAM_USERS:
        raise ValueError("Telegram user allowlist must be a non-empty bounded frozenset")
    if any(
        type(user_id) is not int or not 1 <= user_id <= MAX_TELEGRAM_USER_ID for user_id in value
    ):
        raise ValueError("Telegram user allowlist contains an invalid identifier")
    return value


class TelegramAuthService:
    __slots__ = (
        "_authenticator",
        "_bot_token",
        "_clock",
        "_digester",
        "_allowed_telegram_user_ids",
        "_token_factory",
        "_uow_factory",
    )

    def __init__(
        self,
        *,
        bot_token: str,
        owner_telegram_user_id: int | None = None,
        allowed_telegram_user_ids: frozenset[int] | None = None,
        digester: HttpSecurityDigester,
        uow_factory: AuthUnitOfWorkFactory,
        clock: Callable[[], datetime] = _utc_now,
        token_factory: Callable[[], OpaqueAuthTokens] = generate_auth_tokens,
    ) -> None:
        self._bot_token = bot_token
        if allowed_telegram_user_ids is None:
            if owner_telegram_user_id is None:
                raise ValueError("Telegram user allowlist is required")
            allowed_telegram_user_ids = frozenset((owner_telegram_user_id,))
        elif owner_telegram_user_id is not None:
            raise ValueError("configure one Telegram user policy")
        self._allowed_telegram_user_ids = _validate_allowlist(allowed_telegram_user_ids)
        self._digester = digester
        self._uow_factory = uow_factory
        self._clock = clock
        self._token_factory = token_factory
        self._authenticator = SessionAuthenticator(digester, allowed_telegram_user_ids)

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("auth clock must be timezone-aware")
        return value

    def _verify(self, init_data: str, now: datetime) -> VerifiedTelegramAuth:
        return verify_telegram_init_data(
            init_data,
            bot_token=self._bot_token,
            allowed_telegram_user_ids=self._allowed_telegram_user_ids,
            now=now,
        )

    async def login(self, init_data: str, raw_session_token: str = "") -> LoginResult:
        now = self._now()
        verified = self._verify(init_data, now)

        async with self._uow_factory() as persistence:
            existing_token = None
            existing = None
            if is_canonical_opaque_token(raw_session_token):
                existing_token = self._digester.session(raw_session_token)
                checked = await persistence.lock_session_for_login(existing_token, now=now)
                if checked.status is SessionCheckStatus.ACTIVE:
                    existing = checked.authenticated
            if (
                existing is not None
                and existing.owner.telegram_user_id == verified.telegram_user_id
            ):
                self._authenticator.ensure_allowed(existing)
                return LoginResult(
                    authenticated=existing,
                    session_binding=self._digester.session_binding(raw_session_token),
                )

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
            if existing is not None and existing_token is not None:
                await persistence.revoke_session_for_login(existing_token, now=now)
            tokens = self._token_factory()
            if tokens.session_token == tokens.csrf_token:
                raise RuntimeError("opaque auth tokens must be independent")
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
            session_binding=self._digester.session_binding(tokens.session_token),
            tokens=tokens,
        )

    async def read(self, credentials: SessionCredentials) -> AuthenticatedSession:
        now = self._now()
        async with self._uow_factory() as persistence:
            return await self._authenticator.authenticate_read(
                persistence,
                credentials,
                now=now,
            )

    async def logout(
        self,
        raw_session_token: str,
        raw_session_binding: str,
        raw_csrf_cookie: str,
        raw_csrf_header: str,
    ) -> None:
        now = self._now()
        async with self._uow_factory() as persistence:
            self._authenticator.ensure_bound(raw_session_token, raw_session_binding)
            if (
                not is_canonical_opaque_token(raw_csrf_cookie)
                or not is_canonical_opaque_token(raw_csrf_header)
                or not hmac.compare_digest(raw_csrf_cookie, raw_csrf_header)
            ):
                raise CsrfRejectedError
            checked = await persistence.revoke_session(
                self._digester.session(raw_session_token),
                self._digester.csrf(raw_csrf_cookie),
                now=now,
            )
            if checked.status is SessionCheckStatus.CSRF_FAILED:
                raise CsrfRejectedError
            if checked.status is not SessionCheckStatus.ACTIVE or checked.authenticated is None:
                raise SessionInvalidError
            self._authenticator.ensure_allowed(checked.authenticated)
