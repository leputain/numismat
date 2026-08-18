import re
from pathlib import Path


def test_repository_has_no_common_secret_markers() -> None:
    root = Path(__file__).parents[2]
    ignored = {".env.example", "uv.lock"}
    ignored_directories = {
        ".agents",
        ".git",
        ".hypothesis",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tool-venv",
        ".venv",
        "__pycache__",
        "dist",
        "htmlcov",
        "node_modules",
    }
    for path in root.rglob("*"):
        if not path.is_file() or not ignored_directories.isdisjoint(path.parts):
            continue
        if path.name in ignored:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        assert re.search(r"\b\d{8,12}:[A-Za-z0-9_-]{20,}\b", text) is None
        assert "postgresql://" not in text or "change-me" in text or "finbot-dev-only" in text
