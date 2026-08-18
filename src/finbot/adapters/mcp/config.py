from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url

MCP_DATABASE_URL_SECRET = Path("/run/secrets/database_url")


class McpSettings(BaseSettings):
    """Minimal local-MCP configuration without bot or HTTP secrets."""

    model_config = SettingsConfigDict(
        env_file=None,
        extra="ignore",
        hide_input_in_errors=True,
        populate_by_name=True,
        str_strip_whitespace=True,
    )

    database_url: str = Field(repr=False, validation_alias="DATABASE_URL")
    owner_telegram_user_id: int = Field(
        gt=0,
        le=2**52 - 1,
        repr=False,
        validation_alias="OWNER_TELEGRAM_USER_ID",
    )
    log_level: str = Field(default="INFO", validation_alias="LOG_LEVEL")

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

    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, value: str) -> str:
        normalized = value.upper()
        if normalized not in {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"}:
            raise ValueError("LOG_LEVEL is invalid")
        return normalized

    @classmethod
    def from_secret_or_env(cls) -> McpSettings:
        values: dict[str, object] = {}
        if MCP_DATABASE_URL_SECRET.is_file():
            raw_url = MCP_DATABASE_URL_SECRET.read_text(encoding="utf-8").strip()
            if not raw_url or "\x00" in raw_url:
                raise ValueError("database URL secret is empty or invalid")
            values["database_url"] = raw_url
        return cls.model_validate(values)


__all__ = ["MCP_DATABASE_URL_SECRET", "McpSettings"]
