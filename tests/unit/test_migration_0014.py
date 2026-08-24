import importlib.util
import inspect
from pathlib import Path
from types import ModuleType

from finbot.adapters.database.models import NotificationJob, NotificationPreference


def _migration() -> ModuleType:
    path = Path("migrations/versions/0014_notifications.py")
    spec = importlib.util.spec_from_file_location("migration_0014_notifications", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_notification_migration_is_linear_and_downgrade_is_guarded() -> None:
    migration = _migration()
    source = inspect.getsource(migration)

    assert migration.revision == "0014_notifications"
    assert migration.down_revision == "0013_settings_version"
    assert "Cannot downgrade notifications in offline mode" in source
    assert "EXISTS (SELECT 1 FROM notification_jobs)" in source
    assert "EXISTS (SELECT 1 FROM notification_preferences)" in source


def test_queue_schema_cannot_persist_financial_or_message_content() -> None:
    columns = set(NotificationJob.__table__.c.keys())
    forbidden = {
        "amount",
        "amount_minor",
        "currency",
        "description",
        "message",
        "message_text",
        "telegram_chat_id",
        "telegram_user_id",
    }

    assert not columns & forbidden
    assert {"user_id", "kind", "reference_id", "dedupe_digest"} <= columns
    assert NotificationJob.__table__.c.dedupe_digest.unique
    assert NotificationPreference.__table__.c.user_id.primary_key
    indexes = {index.name: index for index in NotificationJob.__table__.indexes}
    cleanup = indexes["ix_notification_jobs_terminal_cleanup"]
    assert tuple(column.name for column in cleanup.columns) == ("updated_at", "id")
    assert str(cleanup.dialect_options["postgresql"]["where"]) == (
        "status IN ('delivered', 'failed')"
    )

    migration = _migration()
    source = inspect.getsource(migration)
    assert '"ix_notification_jobs_terminal_cleanup"' in source
    assert "status IN ('delivered', 'failed')" in source
