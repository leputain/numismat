import json
import logging

from finbot.observability.logging import JsonFormatter


def test_notification_logs_keep_only_fixed_event_and_safe_result() -> None:
    record = logging.LogRecord(
        "finbot.notification.delivery_process",
        logging.INFO,
        "",
        0,
        "notification_delivery_tick_completed",
        (),
        None,
    )
    record.result = "success"
    record.amount_minor = 123_45
    record.currency = "RUB"
    record.telegram_chat_id = 9_000_004_101

    rendered = JsonFormatter().format(record)

    assert json.loads(rendered) == {
        "component": "notification",
        "correlation_id": "system",
        "event": "notification_delivery_tick_completed",
        "level": "INFO",
        "result": "success",
    }
    assert "12345" not in rendered
    assert "RUB" not in rendered
    assert "9000004101" not in rendered
