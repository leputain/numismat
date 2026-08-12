from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore",
        hide_input_in_errors=True,
        populate_by_name=True,
        str_strip_whitespace=True,
    )
    telegram_bot_token: str = Field(
        min_length=1,
        repr=False,
        validation_alias="TELEGRAM_BOT_TOKEN",
    )
    owner_telegram_user_id: int = Field(
        gt=0,
        le=2**63 - 1,
        validation_alias="OWNER_TELEGRAM_USER_ID",
    )
    database_url: str = Field(
        default="postgresql+psycopg://finbot:finbot@localhost:5432/finbot",
        repr=False,
        validation_alias="DATABASE_URL",
    )
    default_locale: str = Field(
        default="ru_RU",
        min_length=1,
        max_length=16,
        pattern=r"^[A-Za-z0-9_.@-]+$",
    )
    default_timezone: str = Field(default="Europe/Moscow", min_length=1, max_length=64)
    default_currency: str = Field(default="RUB", pattern=r"^[A-Z]{3}$")
    log_level: str = "INFO"

    @field_validator("database_url")
    @classmethod
    def validate_database_url(cls, value: str) -> str:
        try:
            url = make_url(value)
        except Exception as exc:
            raise ValueError("DATABASE_URL must be a valid SQLAlchemy URL") from exc
        if url.drivername != "postgresql+psycopg" or not url.database or not url.username:
            raise ValueError("DATABASE_URL must use postgresql+psycopg with user and database")
        return value

    @field_validator("default_timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ValueError, ZoneInfoNotFoundError) as exc:
            raise ValueError("DEFAULT_TIMEZONE must be a known IANA timezone") from exc
        return value

    @classmethod
    def from_secret_or_env(cls) -> Settings:
        values: dict[str, object] = {}
        for key, path in (
            ("telegram_bot_token", "/run/secrets/telegram_bot_token"),
            ("database_url", "/run/secrets/database_url"),
        ):
            secret = Path(path)
            if secret.is_file():
                values[key] = secret.read_text(encoding="utf-8").strip()
        return cls.model_validate(values)
