from __future__ import annotations

import json
import logging
import sys

from pytest import CaptureFixture, MonkeyPatch

from finbot import healthcheck
from finbot.observability.logging import JsonFormatter, correlation_id


class ExplosiveMessage:
    def __init__(self) -> None:
        self.stringified = False

    def __str__(self) -> str:
        self.stringified = True
        raise AssertionError("free-form log message was stringified")


def test_formatter_does_not_render_message_args_or_unknown_extras() -> None:
    message = ExplosiveMessage()
    sensitive = "123456" + ":" + "synthetic_token_fragment"
    record = logging.LogRecord(
        "untrusted." + sensitive,
        logging.INFO,
        "",
        0,
        message,
        (sensitive, "dinner description", 145050, 999_888_777),
        None,
    )
    record.description = "dinner description"
    record.amount = 145050
    record.telegram_user_id = 999_888_777

    rendered = JsonFormatter().format(record)

    assert message.stringified is False
    assert json.loads(rendered) == {
        "component": "external",
        "correlation_id": "system",
        "event": "log_event_rejected",
        "level": "INFO",
    }
    assert sensitive not in rendered
    assert "description" not in rendered
    assert "145050" not in rendered
    assert "999888777" not in rendered


def test_formatter_emits_only_bounded_known_fields_and_error_class() -> None:
    sensitive = "credential-" + "fragment"
    try:
        raise RuntimeError(sensitive)
    except RuntimeError:
        exc_info = sys.exc_info()

    record = logging.LogRecord(
        "finbot.auth.dynamic-sensitive-suffix",
        logging.ERROR,
        "",
        0,
        "update_failed",
        (),
        exc_info,
    )
    record.result = "error"
    record.event_type = "Message"
    record.duration_ms = 145050
    record.handler = sensitive
    token = correlation_id.set(sensitive)
    try:
        rendered = JsonFormatter().format(record)
    finally:
        correlation_id.reset(token)

    assert json.loads(rendered) == {
        "component": "auth",
        "correlation_id": "invalid",
        "duration": "over_5_s",
        "error_class": "RuntimeError",
        "event": "update_failed",
        "event_type": "message",
        "level": "ERROR",
        "result": "error",
    }
    assert sensitive not in rendered


def test_healthcheck_main_emits_json_success(
    monkeypatch: MonkeyPatch, capsys: CaptureFixture[str]
) -> None:
    async def successful_check() -> None:
        return None

    monkeypatch.setattr(healthcheck, "check", successful_check)
    assert healthcheck.main() == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"status": "ok"}
    assert captured.err == ""


def test_healthcheck_main_never_emits_exception_message(
    monkeypatch: MonkeyPatch, capsys: CaptureFixture[str]
) -> None:
    sensitive = "credential-" + "fragment"

    async def failing_check() -> None:
        raise RuntimeError(sensitive)

    monkeypatch.setattr(healthcheck, "check", failing_check)
    assert healthcheck.main() == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err) == {"error_class": "RuntimeError", "status": "error"}
    assert sensitive not in captured.err
