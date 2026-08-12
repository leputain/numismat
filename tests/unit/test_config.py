import pytest
from pydantic import ValidationError

from finbot.config import Settings

VALID = {
    "telegram_bot_token": "123456:synthetic_test_token",
    "owner_telegram_user_id": 42,
}


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("default_currency", "rub"),
        ("default_currency", "EURO"),
        ("default_timezone", "Mars/Olympus"),
        ("owner_telegram_user_id", 0),
        ("owner_telegram_user_id", 2**63),
        ("telegram_bot_token", ""),
        ("database_url", "sqlite:///tmp/finbot.db"),
        ("default_locale", "x" * 17),
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
    assert str(VALID["telegram_bot_token"]) not in repr(settings)
    assert settings.database_url not in repr(settings)
