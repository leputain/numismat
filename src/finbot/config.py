import base64
import binascii
from pathlib import Path
from typing import Annotated, Self
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict
from sqlalchemy.engine import make_url

_MAX_TELEGRAM_USER_ID = 2**52 - 1
_MAX_ALLOWED_TELEGRAM_USERS = 32
_MAX_TELEGRAM_ALLOWLIST_TEXT_LENGTH = (
    _MAX_ALLOWED_TELEGRAM_USERS * len(str(_MAX_TELEGRAM_USER_ID)) + _MAX_ALLOWED_TELEGRAM_USERS - 1
)
_TELEGRAM_ALLOWLIST_ERROR = (
    "TELEGRAM_ALLOWED_USER_IDS must contain 1 to 32 unique canonical Telegram user IDs"
)


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
        le=_MAX_TELEGRAM_USER_ID,
        repr=False,
        validation_alias="OWNER_TELEGRAM_USER_ID",
    )
    telegram_allowed_user_ids: Annotated[tuple[int, ...] | None, NoDecode] = Field(
        default=None,
        repr=False,
        validation_alias="TELEGRAM_ALLOWED_USER_IDS",
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
    api_host: str = Field(
        default="127.0.0.1",
        min_length=1,
        max_length=253,
        pattern=r"^[A-Za-z0-9_.:-]+$",
        validation_alias="API_HOST",
    )
    api_port: int = Field(default=8080, ge=1024, le=65535, validation_alias="API_PORT")
    miniapp_public_url: str | None = Field(
        default=None,
        max_length=2048,
        validation_alias="MINIAPP_PUBLIC_URL",
    )
    http_security_key: str | None = Field(
        default=None,
        min_length=43,
        max_length=43,
        repr=False,
        validation_alias="HTTP_SECURITY_KEY",
    )
    bank_import_security_key: str | None = Field(
        default=None,
        min_length=43,
        max_length=43,
        repr=False,
        validation_alias="BANK_IMPORT_SECURITY_KEY",
    )
    local_ai_enabled: bool = Field(default=False, validation_alias="LOCAL_AI_ENABLED")
    local_ai_endpoint: str | None = Field(
        default=None,
        max_length=64,
        repr=False,
        validation_alias="LOCAL_AI_ENDPOINT",
    )
    local_ai_model: str | None = Field(
        default=None,
        max_length=128,
        repr=False,
        validation_alias="LOCAL_AI_MODEL",
    )

    @field_validator("telegram_allowed_user_ids", mode="before")
    @classmethod
    def validate_telegram_allowed_user_ids(cls, value: object) -> object:
        if value is None:
            return None
        if type(value) is str:
            if value == "":
                return None
            if len(value) > _MAX_TELEGRAM_ALLOWLIST_TEXT_LENGTH:
                raise ValueError(_TELEGRAM_ALLOWLIST_ERROR)
            components = value.split(",")
            if any(
                not component
                or not component.isascii()
                or not component.isdecimal()
                or (len(component) > 1 and component.startswith("0"))
                for component in components
            ):
                raise ValueError(_TELEGRAM_ALLOWLIST_ERROR)
            user_ids = tuple(int(component) for component in components)
        elif isinstance(value, (list, tuple)):
            if any(type(user_id) is not int for user_id in value):
                raise ValueError(_TELEGRAM_ALLOWLIST_ERROR)
            user_ids = tuple(value)
        else:
            raise ValueError(_TELEGRAM_ALLOWLIST_ERROR)

        if (
            not 1 <= len(user_ids) <= _MAX_ALLOWED_TELEGRAM_USERS
            or any(not 1 <= user_id <= _MAX_TELEGRAM_USER_ID for user_id in user_ids)
            or len(set(user_ids)) != len(user_ids)
        ):
            raise ValueError(_TELEGRAM_ALLOWLIST_ERROR)
        return user_ids

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

    @field_validator("miniapp_public_url")
    @classmethod
    def validate_miniapp_public_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            parsed = urlsplit(value)
            port = parsed.port
        except ValueError as exc:
            raise ValueError("MINIAPP_PUBLIC_URL must be an absolute HTTPS URL") from exc
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path
            or parsed.query
            or parsed.fragment
            or parsed.hostname.endswith(".")
            or parsed.port == 443
            or parsed.hostname != parsed.hostname.encode("idna").decode("ascii")
        ):
            raise ValueError("MINIAPP_PUBLIC_URL must be an absolute HTTPS URL")
        if port is not None and not 1 <= port <= 65535:
            raise ValueError("MINIAPP_PUBLIC_URL contains an invalid port")
        return value

    @field_validator(
        "miniapp_public_url",
        "http_security_key",
        "bank_import_security_key",
        mode="before",
    )
    @classmethod
    def reject_ambiguous_http_security_config(cls, value: object) -> object:
        if type(value) is str and (
            value != value.strip()
            or any(ord(character) < 0x21 or ord(character) == 0x7F for character in value)
        ):
            raise ValueError("HTTP security configuration must use canonical text")
        return value

    @field_validator("http_security_key")
    @classmethod
    def validate_http_security_key(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            decoded = base64.urlsafe_b64decode(value + "=")
        except (ValueError, binascii.Error) as exc:
            raise ValueError("HTTP_SECURITY_KEY must encode exactly 32 random bytes") from exc
        canonical = base64.urlsafe_b64encode(decoded).rstrip(b"=").decode("ascii")
        if len(decoded) != 32 or canonical != value:
            raise ValueError("HTTP_SECURITY_KEY must encode exactly 32 random bytes")
        return value

    @field_validator("bank_import_security_key")
    @classmethod
    def validate_bank_import_security_key(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            decoded = base64.urlsafe_b64decode(value + "=")
        except (ValueError, binascii.Error) as exc:
            raise ValueError(
                "BANK_IMPORT_SECURITY_KEY must encode exactly 32 random bytes"
            ) from exc
        canonical = base64.urlsafe_b64encode(decoded).rstrip(b"=").decode("ascii")
        if len(decoded) != 32 or canonical != value:
            raise ValueError("BANK_IMPORT_SECURITY_KEY must encode exactly 32 random bytes")
        return value

    @field_validator("local_ai_endpoint", "local_ai_model", mode="before")
    @classmethod
    def normalize_optional_local_ai_config(cls, value: object) -> object:
        if value == "":
            return None
        if type(value) is str and value != value.strip():
            raise ValueError("Local AI configuration must use canonical text")
        return value

    @field_validator("local_ai_endpoint")
    @classmethod
    def validate_local_ai_endpoint(cls, value: str | None) -> str | None:
        if value is None:
            return None
        from finbot.adapters.ai.ollama import validate_ollama_endpoint

        return validate_ollama_endpoint(value)

    @field_validator("local_ai_model")
    @classmethod
    def validate_local_ai_model(cls, value: str | None) -> str | None:
        if value is None:
            return None
        from finbot.adapters.ai.ollama import validate_ollama_model

        return validate_ollama_model(value)

    @model_validator(mode="after")
    def require_enabled_local_ai_configuration(self) -> Self:
        if self.local_ai_enabled and (
            self.local_ai_endpoint is None or self.local_ai_model is None
        ):
            raise ValueError(
                "LOCAL_AI_ENDPOINT and LOCAL_AI_MODEL are required when local AI is enabled"
            )
        return self

    @model_validator(mode="after")
    def require_distinct_security_keys(self) -> Self:
        if (
            self.http_security_key is not None
            and self.bank_import_security_key is not None
            and self.http_security_key == self.bank_import_security_key
        ):
            raise ValueError("HTTP_SECURITY_KEY and BANK_IMPORT_SECURITY_KEY must be independent")
        return self

    @model_validator(mode="after")
    def require_primary_owner_in_telegram_allowlist(self) -> Self:
        if (
            self.telegram_allowed_user_ids is not None
            and self.owner_telegram_user_id not in self.telegram_allowed_user_ids
        ):
            raise ValueError("TELEGRAM_ALLOWED_USER_IDS must include OWNER_TELEGRAM_USER_ID")
        return self

    @property
    def miniapp_origin(self) -> str | None:
        if self.miniapp_public_url is None:
            return None
        parsed = urlsplit(self.miniapp_public_url)
        host = parsed.hostname or ""
        if ":" in host:
            host = f"[{host}]"
        port = f":{parsed.port}" if parsed.port is not None and parsed.port != 443 else ""
        return f"https://{host}{port}"

    @property
    def bank_import_key_bytes(self) -> bytes | None:
        if self.bank_import_security_key is None:
            return None
        return base64.urlsafe_b64decode(self.bank_import_security_key + "=")

    @property
    def effective_telegram_user_ids(self) -> frozenset[int]:
        configured = self.telegram_allowed_user_ids
        if configured is None:
            return frozenset((self.owner_telegram_user_id,))
        return frozenset(configured)

    @classmethod
    def from_secret_or_env(cls) -> Settings:
        values: dict[str, object] = {}
        for key, path in (
            ("telegram_bot_token", "/run/secrets/telegram_bot_token"),
            ("database_url", "/run/secrets/database_url"),
            ("http_security_key", "/run/secrets/http_security_key"),
            ("bank_import_security_key", "/run/secrets/bank_import_security_key"),
        ):
            secret = Path(path)
            if secret.is_file():
                values[key] = secret.read_text(encoding="utf-8").strip()
        return cls.model_validate(values)


class RecurringRunnerSettings(BaseSettings):
    """Least-privilege configuration for the DB-only recurring runner."""

    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore",
        hide_input_in_errors=True,
        populate_by_name=True,
        str_strip_whitespace=True,
    )
    database_url: str = Field(
        default="postgresql+psycopg://finbot:finbot@localhost:5432/finbot",
        repr=False,
        validation_alias="DATABASE_URL",
    )
    interval_seconds: int = Field(
        default=30,
        ge=5,
        le=3600,
        validation_alias="RECURRING_TICK_INTERVAL_SECONDS",
    )
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

    @classmethod
    def from_secret_or_env(cls) -> RecurringRunnerSettings:
        values: dict[str, object] = {}
        secret = Path("/run/secrets/database_url")
        if secret.is_file():
            values["database_url"] = secret.read_text(encoding="utf-8").strip()
        return cls.model_validate(values)
