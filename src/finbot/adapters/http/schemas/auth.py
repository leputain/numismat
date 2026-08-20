from datetime import UTC, datetime
from typing import Literal

from pydantic import Field, field_serializer

from finbot.adapters.http.schemas.common import ApiModel


class TelegramAuthRequest(ApiModel):
    initData: str = Field(  # noqa: N815 - Telegram contract
        min_length=1,
        max_length=8192,
        repr=False,
    )


class AuthSessionResponse(ApiModel):
    authenticated: Literal[True] = True
    locale: str = Field(min_length=1, max_length=16)
    timezone: str = Field(min_length=1, max_length=64)
    base_currency: str = Field(pattern=r"^[A-Z]{3}$")
    expires_at: datetime = Field(json_schema_extra={"format": "date-time"})

    @field_serializer("expires_at", when_used="json")
    def _serialize_expires_at(self, value: datetime) -> str:
        utc_value = value.astimezone(UTC)
        return f"{utc_value.isoformat(timespec='milliseconds').removesuffix('+00:00')}Z"
