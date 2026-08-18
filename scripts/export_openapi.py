from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

_SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src"
if str(_SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SOURCE_ROOT))

from finbot.adapters.http.openapi_contract import (  # noqa: E402
    DEFAULT_OPENAPI_OUTPUT,
    export_openapi,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Export the offline deterministic Numismat OpenAPI contract.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OPENAPI_OUTPUT,
        help=f"Output path (default: {DEFAULT_OPENAPI_OUTPUT.as_posix()}).",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    export_openapi(arguments.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
