from pytest import CaptureFixture, MonkeyPatch

from finbot.adapters.http import server
from finbot.config import Settings

VALID = {
    "telegram_bot_token": "123456:synthetic_test_token",
    "owner_telegram_user_id": 42,
    "miniapp_public_url": "https://miniapp.example.test",
    "http_security_key": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "bank_import_security_key": "AQIDBAUGBwgJCgsMDQ4PEBESExQVFhcYGRobHB0eHyA",
}


def test_http_server_disables_access_and_proxy_logging(monkeypatch: MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_run(app: object, **kwargs: object) -> None:
        captured["app"] = app
        captured.update(kwargs)

    monkeypatch.setattr(server.uvicorn, "run", fake_run)
    settings = Settings(**VALID, api_host="127.0.0.1", api_port=9080)

    server.run(settings)

    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == 9080
    assert captured["access_log"] is False
    assert captured["date_header"] is False
    assert captured["log_config"] is None
    assert captured["proxy_headers"] is False
    assert captured["server_header"] is False


def test_http_server_fails_closed_without_http_auth_configuration() -> None:
    settings = Settings(
        telegram_bot_token="123456:synthetic_test_token",
        owner_telegram_user_id=42,
        miniapp_public_url=None,
        http_security_key=None,
    )

    try:
        server.run(settings)
    except RuntimeError as error:
        assert str(error) == "HTTP auth security configuration is required"
    else:
        raise AssertionError("HTTP server must require Mini App auth security configuration")


def test_http_server_main_never_prints_configuration_error_value(
    monkeypatch: MonkeyPatch, capsys: CaptureFixture[str]
) -> None:
    sensitive = "sensitive-config-fragment"

    def fail_settings() -> Settings:
        raise RuntimeError(sensitive)

    monkeypatch.setattr(server.Settings, "from_secret_or_env", fail_settings)

    try:
        server.main()
    except SystemExit as error:
        assert error.code == 1
    else:
        raise AssertionError("server.main() must exit on invalid configuration")

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == '{"error_class":"RuntimeError","status":"error"}\n'
    assert sensitive not in captured.err
