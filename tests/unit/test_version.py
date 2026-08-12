import tomllib
from pathlib import Path

from finbot import VERSION_LABEL, __version__
from finbot.adapters.telegram.presenters import HELP_TEXT


def test_public_release_version_is_consistent() -> None:
    root = Path(__file__).parents[2]
    metadata = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    dockerfile = (root / "Dockerfile").read_text(encoding="utf-8")

    assert __version__ == metadata["project"]["version"] == "0.45.0"
    assert VERSION_LABEL == "v" + ".".join(__version__.split(".")[:2]) == "v0.45"
    assert VERSION_LABEL in HELP_TEXT
    assert f'ARG APP_VERSION="{__version__}"' in dockerfile
