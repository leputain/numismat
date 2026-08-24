from datetime import UTC, datetime, time
from uuid import UUID

import pytest

from finbot.application.notifications import (
    MAX_NOTIFICATION_ATTEMPTS,
    MAX_NOTIFICATION_CLEANUP_BATCH,
    NOTIFICATION_RETENTION_DAYS,
    NotificationDigest,
    NotificationIntent,
    NotificationJobSnapshot,
    NotificationJobStatus,
    NotificationPreferencesSnapshot,
    ReplaceNotificationPreferences,
    budget_notification_kind,
)
from finbot.application.use_cases.notifications import NotificationPreferencesUseCases
from finbot.domain.notifications import (
    NotificationKind,
    NotificationPreferences,
    latest_weekly_slot,
    quiet_until,
)

OWNER_A = UUID("00000000-0000-7000-8000-000000000101")
OWNER_B = UUID("00000000-0000-7000-8000-000000000102")
REFERENCE = UUID("00000000-0000-7000-8000-000000000201")
KEY = bytes(range(32))


class _PreferencesRepository:
    def __init__(self) -> None:
        self.snapshot = NotificationPreferencesSnapshot(
            owner_id=OWNER_A,
            timezone="UTC",
            preferences=NotificationPreferences(),
            version=0,
        )

    async def get_preferences(self, owner_id: UUID) -> NotificationPreferencesSnapshot:
        assert owner_id == OWNER_A
        return self.snapshot

    async def replace_preferences(
        self,
        command: ReplaceNotificationPreferences,
    ) -> NotificationPreferencesSnapshot:
        assert command.owner_id == OWNER_A and command.expected_version == 0
        self.snapshot = NotificationPreferencesSnapshot(
            owner_id=OWNER_A,
            timezone="UTC",
            preferences=command.preferences,
            version=1,
        )
        return self.snapshot


def test_preferences_are_opt_in_and_validate_local_minute_ranges() -> None:
    preferences = NotificationPreferences()
    assert not any(preferences.enabled_for(kind) for kind in NotificationKind)

    with pytest.raises(ValueError, match="задаются вместе"):
        NotificationPreferences(quiet_start=time(22, 0))
    with pytest.raises(ValueError, match="точность до минуты"):
        NotificationPreferences(weekly_time=time(9, 0, 1))
    with pytest.raises(ValueError, match="полные сутки"):
        NotificationPreferences(quiet_start=time(22, 0), quiet_end=time(22, 0))


@pytest.mark.asyncio
async def test_preferences_use_case_exposes_absent_version_zero_then_cas_version_one() -> None:
    repository = _PreferencesRepository()
    use_cases = NotificationPreferencesUseCases(repository)

    initial = await use_cases.get(OWNER_A)
    updated = await use_cases.replace(
        ReplaceNotificationPreferences(
            owner_id=OWNER_A,
            expected_version=0,
            preferences=NotificationPreferences(weekly_digest_enabled=True),
        )
    )

    assert initial.version == 0
    assert updated.version == 1
    assert updated.preferences.weekly_digest_enabled


def test_quiet_hours_support_overnight_boundaries_without_consuming_locality() -> None:
    preferences = NotificationPreferences(quiet_start=time(22), quiet_end=time(7))

    assert (
        quiet_until(
            preferences,
            timezone="UTC",
            now=datetime(2026, 8, 24, 21, 59, tzinfo=UTC),
        )
        is None
    )
    assert quiet_until(
        preferences,
        timezone="UTC",
        now=datetime(2026, 8, 24, 22, 0, tzinfo=UTC),
    ) == datetime(2026, 8, 25, 7, 0, tzinfo=UTC)
    assert quiet_until(
        preferences,
        timezone="UTC",
        now=datetime(2026, 8, 25, 6, 59, tzinfo=UTC),
    ) == datetime(2026, 8, 25, 7, 0, tzinfo=UTC)
    assert (
        quiet_until(
            preferences,
            timezone="UTC",
            now=datetime(2026, 8, 25, 7, 0, tzinfo=UTC),
        )
        is None
    )


def test_weekly_slot_uses_latest_reached_local_slot_and_iso_week_key() -> None:
    preferences = NotificationPreferences(weekly_weekday=0, weekly_time=time(9))

    before = latest_weekly_slot(
        preferences,
        timezone="Europe/Moscow",
        now=datetime(2026, 8, 24, 5, 59, tzinfo=UTC),
    )
    after = latest_weekly_slot(
        preferences,
        timezone="Europe/Moscow",
        now=datetime(2026, 8, 24, 6, 0, tzinfo=UTC),
    )

    assert before.due_at == datetime(2026, 8, 17, 6, 0, tzinfo=UTC)
    assert before.period_key == "2026-08-17"
    assert after.due_at == datetime(2026, 8, 24, 6, 0, tzinfo=UTC)
    assert after.period_key == "2026-08-24"


def test_budget_thresholds_use_actual_integer_spend_and_100_wins() -> None:
    preferences = NotificationPreferences(
        budget_80_enabled=True,
        budget_100_enabled=True,
    )

    assert (
        budget_notification_kind(
            spent_minor=799,
            limit_minor=1_000,
            preferences=preferences,
        )
        is None
    )
    assert (
        budget_notification_kind(
            spent_minor=800,
            limit_minor=1_000,
            preferences=preferences,
        )
        is NotificationKind.BUDGET_80
    )
    assert (
        budget_notification_kind(
            spent_minor=1_000,
            limit_minor=1_000,
            preferences=preferences,
        )
        is NotificationKind.BUDGET_100
    )


def test_keyed_dedupe_is_exactly_32_bytes_and_domain_separates_components() -> None:
    digest = NotificationDigest(KEY)
    base = NotificationIntent(
        owner_id=OWNER_A,
        kind=NotificationKind.BUDGET_80,
        reference_id=REFERENCE,
        period_key="2026-08-01_2026-08-31",
    )
    variants = (
        NotificationIntent(
            owner_id=OWNER_B,
            kind=base.kind,
            reference_id=base.reference_id,
            period_key=base.period_key,
        ),
        NotificationIntent(
            owner_id=base.owner_id,
            kind=NotificationKind.BUDGET_100,
            reference_id=base.reference_id,
            period_key=base.period_key,
        ),
        NotificationIntent(
            owner_id=base.owner_id,
            kind=base.kind,
            reference_id=base.reference_id,
            period_key="2026-09-01_2026-09-30",
        ),
    )

    base_digest = digest.for_intent(base)
    assert len(base_digest) == 32
    assert base_digest == digest.for_intent(base)
    assert all(digest.for_intent(variant) != base_digest for variant in variants)
    assert str(OWNER_A).encode() not in base_digest


def test_job_snapshot_requires_a_lease_token_for_leased_state() -> None:
    with pytest.raises(ValueError, match="lease token"):
        NotificationJobSnapshot(
            job_id=REFERENCE,
            owner_id=OWNER_A,
            kind=NotificationKind.RECURRING_READY,
            available_at=datetime(2026, 8, 24, tzinfo=UTC),
            reference_id=REFERENCE,
            status=NotificationJobStatus.LEASED,
            attempt_count=MAX_NOTIFICATION_ATTEMPTS,
        )


def test_terminal_job_cleanup_has_a_conservative_fixed_bound_and_retention() -> None:
    assert 1 <= MAX_NOTIFICATION_CLEANUP_BATCH <= 500
    assert NOTIFICATION_RETENTION_DAYS >= 400
