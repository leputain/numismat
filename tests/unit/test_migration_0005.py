from importlib.util import module_from_spec, spec_from_file_location
from inspect import getsource
from pathlib import Path
from types import ModuleType


def _load_migration() -> ModuleType:
    path = Path(__file__).parents[2] / "migrations" / "versions" / "0005_channel_neutral_drafts.py"
    spec = spec_from_file_location("finbot_migration_0005", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load migration 0005")
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_channel_neutral_draft_migration_follows_head_and_has_roundtrip() -> None:
    migration = _load_migration()

    assert migration.revision == "0005_channel_neutral_drafts"
    assert migration.down_revision == "0004_telegram_response_outbox"
    assert callable(migration.upgrade)
    assert callable(migration.downgrade)


def test_rolling_migration_keeps_legacy_fields_and_backfills_safe_values() -> None:
    migration = _load_migration()
    upgrade_source = getsource(migration.upgrade)

    assert '"telegram_draft_presentations"' in upgrade_source
    assert 'sa.Column("draft_revision", sa.Integer())' in upgrade_source
    assert 'sa.Column("history_page", sa.Integer())' in upgrade_source
    assert 'sa.Column("pending_history_page", sa.Integer())' in upgrade_source
    assert "history_page BETWEEN 0 AND 1295" in upgrade_source
    assert "pending_history_page BETWEEN 0 AND 1295" in upgrade_source
    assert "telegram_response_outbox_presentation_context_check" in upgrade_source
    assert "draft_revision IS NOT NULL" in upgrade_source
    assert "draft.payload ->> 'ui_message_id'" in upgrade_source
    assert "draft.presentation_ref" in upgrade_source
    assert upgrade_source.index("draft.payload ->> 'ui_message_id'") < upgrade_source.index(
        "draft.presentation_ref"
    )
    assert "app_user.telegram_chat_id IS NOT NULL" in upgrade_source
    assert 'drop_column("drafts"' not in upgrade_source
    assert 'drop_column("telegram_response_outbox", "draft_revision")' not in upgrade_source


def test_downgrade_restores_current_legacy_binding_before_removing_projection() -> None:
    migration = _load_migration()
    downgrade_source = getsource(migration.downgrade)

    assert "UPDATE drafts AS draft" in downgrade_source
    assert "presentation_ref = presentation.message_id::text" in downgrade_source
    assert "'{ui_message_id}'" in downgrade_source
    assert "presentation.rendered_revision = draft.revision" in downgrade_source
    assert downgrade_source.index("UPDATE drafts AS draft") < downgrade_source.index(
        'drop_table("telegram_draft_presentations")'
    )
    assert 'drop_table("telegram_draft_presentations")' in downgrade_source
    assert 'drop_column("telegram_response_outbox", "draft_revision")' in downgrade_source
    assert 'drop_column("telegram_response_outbox", "history_page")' in downgrade_source
    assert 'drop_column("telegram_response_outbox", "pending_history_page")' in downgrade_source
    assert 'add_column("drafts"' not in downgrade_source
