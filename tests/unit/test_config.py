import pytest
from pydantic import ValidationError

from finbot.config import Settings

VALID = {
    "telegram_bot_token": "123456:synthetic_test_token",
    "owner_telegram_user_id": 42,
}
HTTP_SECURITY_KEY = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("default_currency", "rub"),
        ("default_currency", "EURO"),
        ("default_timezone", "Mars/Olympus"),
        ("owner_telegram_user_id", 0),
        ("owner_telegram_user_id", 2**63),
        ("owner_telegram_user_id", 2**52),
        ("telegram_bot_token", ""),
        ("database_url", "sqlite:///tmp/finbot.db"),
        ("default_locale", "x" * 17),
        ("api_host", "host name"),
        ("api_port", 1023),
        ("api_port", 65536),
        ("miniapp_public_url", "http://miniapp.example.test"),
        ("miniapp_public_url", "https://miniapp.example.test/path"),
        ("miniapp_public_url", "https://miniapp.example.test/"),
        ("miniapp_public_url", "https://miniapp.example.test:443"),
        ("http_security_key", "too-short"),
        ("http_security_key", f" {HTTP_SECURITY_KEY}"),
    ],
)
def test_invalid_runtime_configuration_is_rejected(field: str, value: object) -> None:
    values: dict[str, object] = {**VALID, field: value}
    with pytest.raises(ValidationError) as captured:
        Settings(**values)  # type: ignore[arg-type]

    rendered = str(captured.value)
    assert "input_value" not in rendered
    assert VALID["telegram_bot_token"] not in rendered


def test_valid_runtime_configuration_is_normalized_and_accepted() -> None:
    settings = Settings(
        **VALID,
        default_currency="USD",
        default_timezone="America/New_York",
    )

    assert settings.default_currency == "USD"
    assert settings.default_timezone == "America/New_York"
    assert settings.api_host == "127.0.0.1"
    assert settings.api_port == 8080
    assert settings.miniapp_public_url is None
    assert settings.http_security_key is None
    assert str(VALID["telegram_bot_token"]) not in repr(settings)
    assert settings.database_url not in repr(settings)


def test_http_auth_configuration_is_validated_and_origin_is_exact() -> None:
    settings = Settings(
        **VALID,
        miniapp_public_url="https://miniapp.example.test:8443",
        http_security_key=HTTP_SECURITY_KEY,
    )

    assert settings.miniapp_origin == "https://miniapp.example.test:8443"
    assert HTTP_SECURITY_KEY not in repr(settings)
