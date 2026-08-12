import ast
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
LAYER_ROOTS = (PROJECT_ROOT / "src/finbot/domain", PROJECT_ROOT / "src/finbot/application")
FORBIDDEN_PREFIXES = ("aiogram", "sqlalchemy", "finbot.adapters")


def _python_files() -> list[Path]:
    return sorted(path for root in LAYER_ROOTS for path in root.rglob("*.py"))


@pytest.mark.parametrize(
    "path", _python_files(), ids=lambda path: str(path.relative_to(PROJECT_ROOT))
)
def test_domain_and_application_have_no_framework_or_adapter_imports(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    violations: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports = ((node.lineno, alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports = ((node.lineno, node.module),)
        else:
            continue
        violations.extend(
            (line, module) for line, module in imports if module.startswith(FORBIDDEN_PREFIXES)
        )

    assert violations == [], f"forbidden layer dependencies in {path}: {violations}"
