import asyncio
import json
import sys

from finbot.bootstrap import run
from finbot.config import Settings
from finbot.observability.logging import configure, safe_error_class


def main() -> None:
    try:
        settings = Settings.from_secret_or_env()
        configure(settings.log_level)
        asyncio.run(run(settings))
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
