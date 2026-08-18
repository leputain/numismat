import ast
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DOMAIN_ROOT = PROJECT_ROOT / "src/finbot/domain"
APPLICATION_ROOT = PROJECT_ROOT / "src/finbot/application"
TELEGRAM_CONTROLLERS_ROOT = PROJECT_ROOT / "src/finbot/adapters/telegram/controllers"
TELEGRAM_ROUTERS_ROOT = PROJECT_ROOT / "src/finbot/adapters/telegram/routers"
HTTP_ROUTES_ROOT = PROJECT_ROOT / "src/finbot/adapters/http/routes"
HTTP_AUTH_ROOT = PROJECT_ROOT / "src/finbot/adapters/http/auth"
LAYER_ROOTS = (DOMAIN_ROOT, APPLICATION_ROOT)
INNER_LAYER_FORBIDDEN_PREFIXES = (
    "aiogram",
    "fastapi",
    "pydantic",
    "sqlalchemy",
    "starlette",
    "finbot.adapters",
    "finbot.bootstrap",
    "finbot.config",
)
DOMAIN_FORBIDDEN_PREFIXES = ("finbot.application",)
TELEGRAM_CONTROLLER_FORBIDDEN_PREFIXES = (
    "sqlalchemy",
    "finbot.adapters.database",
    "finbot.bootstrap",
)
HTTP_ROUTE_FORBIDDEN_PREFIXES = (
    "sqlalchemy",
    "finbot.adapters.database",
    "finbot.bootstrap",
)


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
        forbidden = INNER_LAYER_FORBIDDEN_PREFIXES
        if path.is_relative_to(DOMAIN_ROOT):
            forbidden += DOMAIN_FORBIDDEN_PREFIXES
        violations.extend(
            (line, module) for line, module in imports if module.startswith(forbidden)
        )

    assert violations == [], f"forbidden layer dependencies in {path}: {violations}"


@pytest.mark.parametrize(
    "path",
    sorted(
        path
        for root in (TELEGRAM_CONTROLLERS_ROOT, TELEGRAM_ROUTERS_ROOT)
        for path in root.rglob("*.py")
    ),
    ids=lambda path: str(path.relative_to(PROJECT_ROOT)),
)
def test_telegram_boundaries_do_not_import_database_or_composition_root(path: Path) -> None:
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
            (line, module)
            for line, module in imports
            if module.startswith(TELEGRAM_CONTROLLER_FORBIDDEN_PREFIXES)
        )

    assert violations == [], f"database dependency in Telegram boundary {path}: {violations}"


@pytest.mark.parametrize(
    "path",
    sorted(HTTP_ROUTES_ROOT.rglob("*.py")),
    ids=lambda path: str(path.relative_to(PROJECT_ROOT)),
)
def test_http_routes_do_not_import_database_or_composition_root(path: Path) -> None:
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
            (line, module)
            for line, module in imports
            if module.startswith(HTTP_ROUTE_FORBIDDEN_PREFIXES)
        )

    assert violations == [], f"database dependency in HTTP route {path}: {violations}"


def test_http_auth_contracts_do_not_import_sqlalchemy_or_composition_root() -> None:
    violations: list[tuple[str, int, str]] = []
    for path in sorted(HTTP_AUTH_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports = ((node.lineno, alias.name) for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports = ((node.lineno, node.module),)
            else:
                continue
            violations.extend(
                (str(path.relative_to(PROJECT_ROOT)), line, module)
                for line, module in imports
                if module.startswith(("sqlalchemy", "finbot.bootstrap"))
            )

    assert violations == []
