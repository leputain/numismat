from __future__ import annotations

from typing import Annotated, Self

from pydantic import ConfigDict, Field, TypeAdapter, model_validator

from finbot.adapters.http.schemas.common import ApiModel
from finbot.application.notifications import NotificationPreferencesSnapshot

LocalMinuteString = Annotated[
    str,
    Field(
        strict=True,
        min_length=5,
        max_length=5,
        pattern=r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$",
    ),
]
NotificationVersion = Annotated[int, Field(strict=True, ge=0, le=2**31 - 1)]
Weekday = Annotated[int, Field(strict=True, ge=0, le=6)]


class NotificationPreferencesRequest(ApiModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    budget_80_enabled: bool
    budget_100_enabled: bool
    recurring_ready_enabled: bool
    weekly_digest_enabled: bool
    quiet_start: LocalMinuteString | None = Field(repr=False)
    quiet_end: LocalMinuteString | None = Field(repr=False)
    weekly_weekday: Weekday
    weekly_time: LocalMinuteString = Field(repr=False)
    version: NotificationVersion = Field(repr=False)

    @model_validator(mode="after")
    def _validate_quiet_pair(self) -> Self:
        if (self.quiet_start is None) != (self.quiet_end is None):
            raise ValueError("quiet hours must be provided as a pair")
        if self.quiet_start is not None and self.quiet_start == self.quiet_end:
            raise ValueError("quiet hours cannot cover a full day")
        return self


NOTIFICATION_PREFERENCES_ADAPTER = TypeAdapter[NotificationPreferencesRequest](
    NotificationPreferencesRequest
)


class NotificationPreferencesResponse(ApiModel):
    budget_80_enabled: bool
    budget_100_enabled: bool
    recurring_ready_enabled: bool
    weekly_digest_enabled: bool
    quiet_start: LocalMinuteString | None = Field(repr=False)
    quiet_end: LocalMinuteString | None = Field(repr=False)
    weekly_weekday: Weekday
    weekly_time: LocalMinuteString = Field(repr=False)
    version: NotificationVersion = Field(repr=False)


def notification_preferences_response(
    snapshot: NotificationPreferencesSnapshot,
) -> NotificationPreferencesResponse:
    preferences = snapshot.preferences
    return NotificationPreferencesResponse(
        budget_80_enabled=preferences.budget_80_enabled,
        budget_100_enabled=preferences.budget_100_enabled,
        recurring_ready_enabled=preferences.recurring_ready_enabled,
        weekly_digest_enabled=preferences.weekly_digest_enabled,
        quiet_start=(
            preferences.quiet_start.isoformat(timespec="minutes")
            if preferences.quiet_start is not None
            else None
        ),
        quiet_end=(
            preferences.quiet_end.isoformat(timespec="minutes")
            if preferences.quiet_end is not None
            else None
        ),
        weekly_weekday=preferences.weekly_weekday,
        weekly_time=preferences.weekly_time.isoformat(timespec="minutes"),
        version=snapshot.version,
    )
