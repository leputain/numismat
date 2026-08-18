import json
import sys

import uvicorn

from finbot.adapters.http.app import create_runtime_app
from finbot.config import Settings
from finbot.observability.logging import configure, safe_error_class


def run(settings: Settings) -> None:
    if settings.http_security_key is None or settings.miniapp_origin is None:
        raise RuntimeError("HTTP auth security configuration is required")
    uvicorn.run(
        create_runtime_app(settings=settings),
        host=settings.api_host,
        port=settings.api_port,
        access_log=False,
        date_header=False,
        log_config=None,
        proxy_headers=False,
        server_header=False,
        use_colors=False,
    )


def main() -> None:
    try:
        settings = Settings.from_secret_or_env()
        configure(settings.log_level)
        run(settings)
    except Exception as exc:
        print(
            json.dumps(
                {"error_class": safe_error_class(exc), "status": "error"},
                separators=(",", ":"),
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
