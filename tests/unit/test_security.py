import re
import subprocess
from pathlib import Path


def _project_files(root: Path) -> tuple[Path, ...]:
    listed = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=root,
        check=False,
        capture_output=True,
    )
    if listed.returncode == 0:
        return tuple(
            root / raw.decode("utf-8", errors="surrogateescape")
            for raw in listed.stdout.split(b"\0")
            if raw
        )
    return tuple(root.rglob("*"))


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
    for path in _project_files(root):
        if not path.is_file() or not ignored_directories.isdisjoint(path.parts):
            continue
        if path.name == ".env" or path.name.startswith(".env.local"):
            continue
        if path.name in ignored:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        assert re.search(r"\b\d{8,12}:[A-Za-z0-9_-]{20,}\b", text) is None
        assert "postgresql://" not in text or "change-me" in text or "finbot-dev-only" in text
