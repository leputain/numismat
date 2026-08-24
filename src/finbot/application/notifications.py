from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from finbot.domain.notifications import NotificationKind, NotificationPreferences

MAX_NOTIFICATION_OWNERS_PER_TICK = 64
MAX_NOTIFICATION_REFS_PER_OWNER = 32
MAX_NOTIFICATION_CLAIM_BATCH = 16
MAX_NOTIFICATION_ATTEMPTS = 5
NOTIFICATION_LEASE_SECONDS = 120
MAX_NOTIFICATION_CLEANUP_BATCH = 500
NOTIFICATION_RETENTION_DAYS = 400


class NotificationJobStatus(StrEnum):
    PENDING = "pending"
    LEASED = "leased"
    DELIVERED = "delivered"
    FAILED = "failed"


class NotificationFailureCode(StrEnum):
    CHAT_INVALID = "chat_invalid"
    OWNER_REVOKED = "owner_revoked"
    PREFERENCE_DISABLED = "preference_disabled"
    REFERENCE_INVALID = "reference_invalid"
    RETRY_EXHAUSTED = "retry_exhausted"
    TELEGRAM_REJECTED = "telegram_rejected"
    TELEGRAM_RETRYABLE = "telegram_retryable"


@dataclass(frozen=True, slots=True, repr=False)
class NotificationPreferencesSnapshot:
    owner_id: UUID = field(repr=False)
    timezone: str = field(repr=False)
    preferences: NotificationPreferences = field(repr=False)
    version: int

    def __post_init__(self) -> None:
        if not isinstance(self.owner_id, UUID):
            raise ValueError("Владелец настроек уведомлений не прошёл проверку")
        if not isinstance(self.timezone, str) or not self.timezone:
            raise ValueError("Часовой пояс настроек уведомлений не прошёл проверку")
        if not isinstance(self.preferences, NotificationPreferences):
            raise ValueError("Настройки уведомлений не прошли проверку")
        if isinstance(self.version, bool) or not isinstance(self.version, int) or self.version < 0:
            raise ValueError("Версия настроек уведомлений не прошла проверку")


@dataclass(frozen=True, slots=True, repr=False)
class ReplaceNotificationPreferences:
    owner_id: UUID = field(repr=False)
    expected_version: int
    preferences: NotificationPreferences = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.owner_id, UUID):
            raise ValueError("Владелец настроек уведомлений не прошёл проверку")
        if (
            isinstance(self.expected_version, bool)
            or not isinstance(self.expected_version, int)
            or self.expected_version < 0
        ):
            raise ValueError("Версия настроек уведомлений не прошла проверку")
        if not isinstance(self.preferences, NotificationPreferences):
            raise ValueError("Настройки уведомлений не прошли проверку")


@dataclass(frozen=True, slots=True, repr=False)
class NotificationIntent:
    owner_id: UUID = field(repr=False)
    kind: NotificationKind
    period_key: str = field(repr=False)
    reference_id: UUID | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.owner_id, UUID) or not isinstance(self.kind, NotificationKind):
            raise ValueError("Намерение уведомления не прошло проверку")
        if (
            not isinstance(self.period_key, str)
            or not self.period_key
            or len(self.period_key) > 64
            or not self.period_key.isascii()
            or not self.period_key.isprintable()
        ):
            raise ValueError("Период уведомления не прошёл проверку")
        if self.kind is NotificationKind.WEEKLY_DIGEST:
            if self.reference_id is not None:
                raise ValueError("Еженедельная сводка не принимает ссылку")
        elif not isinstance(self.reference_id, UUID):
            raise ValueError("Ссылка уведомления не прошла проверку")


@dataclass(frozen=True, slots=True, repr=False)
class NotificationJobSnapshot:
    job_id: UUID = field(repr=False)
    owner_id: UUID = field(repr=False)
    kind: NotificationKind
    available_at: datetime = field(repr=False)
    reference_id: UUID | None = field(default=None, repr=False)
    status: NotificationJobStatus = NotificationJobStatus.PENDING
    attempt_count: int = 0
    lease_token: UUID | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.job_id, UUID) or not isinstance(self.owner_id, UUID):
            raise ValueError("Задание уведомления не прошло проверку")
        if not isinstance(self.kind, NotificationKind):
            raise ValueError("Тип задания уведомления не прошёл проверку")
        if not isinstance(self.status, NotificationJobStatus):
            raise ValueError("Статус задания уведомления не прошёл проверку")
        if (
            isinstance(self.attempt_count, bool)
            or not isinstance(self.attempt_count, int)
            or not 0 <= self.attempt_count <= MAX_NOTIFICATION_ATTEMPTS
        ):
            raise ValueError("Число попыток уведомления не прошло проверку")
        if self.available_at.utcoffset() is None:
            raise ValueError("Время задания уведомления должно содержать timezone")
        if self.status is NotificationJobStatus.LEASED and self.lease_token is None:
            raise ValueError("Арендованное задание должно содержать lease token")


@dataclass(frozen=True, slots=True, repr=False)
class NotificationDeliveryContext:
    job: NotificationJobSnapshot = field(repr=False)
    telegram_user_id: int = field(repr=False)
    telegram_chat_id: int | None = field(repr=False)
    timezone: str = field(repr=False)
    preferences: NotificationPreferences = field(repr=False)


class NotificationPreferencesRepository(Protocol):
    async def get_preferences(self, owner_id: UUID) -> NotificationPreferencesSnapshot: ...

    async def replace_preferences(
        self,
        command: ReplaceNotificationPreferences,
    ) -> NotificationPreferencesSnapshot: ...


class NotificationSendFailure(RuntimeError):
    __slots__ = ("retryable",)

    def __init__(self, *, retryable: bool) -> None:
        super().__init__("notification delivery failed")
        self.retryable = retryable


class NotificationSender(Protocol):
    async def send(self, *, chat_id: int, text: str) -> None: ...


class NotificationDigest:
    """Domain-separated keyed digest; raw dedupe components never reach persistence."""

    __slots__ = ("_key",)

    def __init__(self, key: bytes) -> None:
        if type(key) is not bytes or len(key) != 32:
            raise ValueError("Ключ дедупликации уведомлений должен содержать 32 байта")
        self._key = key

    def for_intent(self, intent: NotificationIntent) -> bytes:
        reference = intent.reference_id.bytes if intent.reference_id is not None else bytes(16)
        payload = b"\x00".join(
            (
                b"finbot-notification-v1",
                intent.owner_id.bytes,
                intent.kind.value.encode("ascii"),
                reference,
                intent.period_key.encode("ascii"),
            )
        )
        return hmac.new(self._key, payload, hashlib.sha256).digest()


def budget_notification_kind(
    *,
    spent_minor: int,
    limit_minor: int,
    preferences: NotificationPreferences,
) -> NotificationKind | None:
    """Choose one threshold using actual integer spend; 100% wins over 80%."""

    for value in (spent_minor, limit_minor):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("Порог бюджета должен быть неотрицательным целым числом")
    if limit_minor < 1:
        raise ValueError("Лимит бюджета должен быть положительным")
    if preferences.budget_100_enabled and spent_minor >= limit_minor:
        return NotificationKind.BUDGET_100
    if preferences.budget_80_enabled and spent_minor * 100 >= limit_minor * 80:
        return NotificationKind.BUDGET_80
    return None


STATIC_NOTIFICATION_TEXT = {
    NotificationKind.BUDGET_80: (
        "Расходы по одному из бюджетов достигли 80%. Откройте приложение, чтобы проверить план."
    ),
    NotificationKind.BUDGET_100: (
        "Лимит одного из бюджетов достигнут. Откройте приложение, чтобы проверить расходы."
    ),
    NotificationKind.RECURRING_READY: (
        "Готова регулярная операция. Откройте приложение и подтвердите черновик."
    ),
    NotificationKind.WEEKLY_DIGEST: (
        "Еженедельная финансовая сводка готова. Откройте приложение, чтобы посмотреть её."
    ),
}
