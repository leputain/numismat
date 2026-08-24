import pytest
from pydantic import ValidationError

from finbot.config import NotificationDeliverySettings, NotificationSchedulerSettings

KEY = "AQIDBAUGBwgJCgsMDQ4PEBESExQVFhcYGRobHB0eHyA"


def test_scheduler_requires_a_canonical_independent_32_byte_key() -> None:
    settings = NotificationSchedulerSettings(
        _env_file=None,
        notification_security_key=KEY,
    )

    assert settings.notification_key_bytes == bytes(range(1, 33))
    assert KEY not in repr(settings)
    with pytest.raises(ValidationError) as captured:
        NotificationSchedulerSettings(
            _env_file=None,
            notification_security_key=f" {KEY}",
        )
    assert "input_value" not in str(captured.value)


def test_delivery_settings_reuse_the_bounded_allowlist_without_digest_key() -> None:
    settings = NotificationDeliverySettings(
        _env_file=None,
        telegram_bot_token="123456:synthetic_test_token",
        owner_telegram_user_id=42,
        telegram_allowed_user_ids="42,43",
    )

    assert settings.effective_telegram_user_ids == frozenset((42, 43))
    fields = set(type(settings).model_fields)
    assert fields == {
        "database_url",
        "interval_seconds",
        "log_level",
        "owner_telegram_user_id",
        "telegram_allowed_user_ids",
        "telegram_bot_token",
    }
    assert (
        not {
            "notification_security_key",
            "http_security_key",
            "bank_import_security_key",
            "local_ai_enabled",
            "local_ai_endpoint",
            "local_ai_model",
            "miniapp_public_url",
        }
        & fields
    )
