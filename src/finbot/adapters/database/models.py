from datetime import datetime
from uuid import UUID, uuid7

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid7)
    telegram_user_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    telegram_chat_id: Mapped[int | None] = mapped_column(BigInteger)
    locale: Mapped[str] = mapped_column(String(16), default="ru_RU")
    timezone: Mapped[str] = mapped_column(String(64), default="Europe/Moscow")
    base_currency: Mapped[str] = mapped_column(String(3), default="RUB")
    fast_mode: Mapped[bool] = mapped_column(Boolean, default=True)
    default_account_id: Mapped[UUID | None] = mapped_column(
        ForeignKey(
            "accounts.id",
            use_alter=True,
            name="fk_users_default_account_id",
            ondelete="SET NULL",
        )
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Account(Base):
    __tablename__ = "accounts"
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid7)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    name: Mapped[str] = mapped_column(String(100))
    slug: Mapped[str] = mapped_column(String(100))
    type: Mapped[str] = mapped_column(String(20), default="card")
    currency: Mapped[str] = mapped_column(String(3), default="RUB")
    initial_balance_minor: Mapped[int] = mapped_column(BigInteger, default=0)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    __table_args__ = (
        UniqueConstraint("user_id", "slug"),
        UniqueConstraint("id", "user_id", name="uq_accounts_id_user_id"),
    )


class Category(Base):
    __tablename__ = "categories"
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid7)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    kind: Mapped[str] = mapped_column(String(10))
    name: Mapped[str] = mapped_column(String(100))
    slug: Mapped[str] = mapped_column(String(100))
    emoji: Mapped[str] = mapped_column(String(8), default="")
    parent_id: Mapped[UUID | None] = mapped_column(ForeignKey("categories.id"))
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(Integer, default=1, server_default=text("1"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    __table_args__ = (
        UniqueConstraint("user_id", "kind", "slug"),
        UniqueConstraint("id", "user_id", "kind", name="uq_categories_id_user_kind"),
        CheckConstraint("kind IN ('expense', 'income')", name="categories_kind_check"),
        CheckConstraint("version >= 1", name="categories_version_check"),
    )


class Transaction(Base):
    __tablename__ = "transactions"
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid7)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    type: Mapped[str] = mapped_column(String(10))
    amount_minor: Mapped[int] = mapped_column(BigInteger)
    currency: Mapped[str] = mapped_column(String(3))
    account_id: Mapped[UUID] = mapped_column(ForeignKey("accounts.id"))
    category_id: Mapped[UUID] = mapped_column(ForeignKey("categories.id"))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    description: Mapped[str] = mapped_column(String(500), default="")
    source: Mapped[str] = mapped_column(String(20), default="manual")
    telegram_update_id: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(Integer, default=1)
    __table_args__ = (
        CheckConstraint("amount_minor > 0", name="transactions_amount_minor_check"),
        CheckConstraint(
            "amount_minor <= 9223372036854775807",
            name="transactions_amount_minor_bigint_check",
        ),
        CheckConstraint("type IN ('expense', 'income')", name="transactions_type_check"),
        CheckConstraint("currency ~ '^[A-Z]{3}$'", name="transactions_currency_check"),
        ForeignKeyConstraint(
            ["account_id", "user_id"],
            ["accounts.id", "accounts.user_id"],
            name="fk_transactions_account_owner",
        ),
        ForeignKeyConstraint(
            ["category_id", "user_id", "type"],
            ["categories.id", "categories.user_id", "categories.kind"],
            name="fk_transactions_category_owner_kind",
        ),
        Index(
            "uq_transactions_telegram_update_id",
            "telegram_update_id",
            unique=True,
            postgresql_where=text("telegram_update_id IS NOT NULL"),
        ),
        Index(
            "ix_transactions_user_occurred_active",
            "user_id",
            "occurred_at",
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index(
            "ix_transactions_user_deleted",
            "user_id",
            "deleted_at",
            postgresql_where=text("deleted_at IS NOT NULL"),
        ),
    )


class ProcessedUpdate(Base):
    __tablename__ = "processed_updates"
    update_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    processed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class TelegramResponseOutbox(Base):
    __tablename__ = "telegram_response_outbox"
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid7)
    update_id: Mapped[int] = mapped_column(
        ForeignKey("processed_updates.update_id", ondelete="CASCADE"), nullable=False
    )
    sequence: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    owner_telegram_user_id: Mapped[int] = mapped_column(BigInteger)
    chat_id: Mapped[int] = mapped_column(BigInteger)
    method: Mapped[str] = mapped_column(String(24))
    message_id: Mapped[int | None] = mapped_column(BigInteger)
    body: Mapped[str] = mapped_column("text", Text)
    parse_mode: Mapped[str | None] = mapped_column(String(16))
    reply_markup: Mapped[dict[str, object] | None] = mapped_column(JSONB(none_as_null=True))
    draft_id: Mapped[UUID | None] = mapped_column(ForeignKey("drafts.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        UniqueConstraint(
            "update_id", "sequence", name="uq_telegram_response_outbox_update_sequence"
        ),
        CheckConstraint("sequence >= 0", name="telegram_response_outbox_sequence_check"),
        CheckConstraint(
            "method IN ('send_message', 'edit_message_text')",
            name="telegram_response_outbox_method_check",
        ),
        CheckConstraint(
            "(method = 'send_message' AND message_id IS NULL) "
            "OR (method = 'edit_message_text' AND message_id > 0)",
            name="telegram_response_outbox_target_check",
        ),
        CheckConstraint(
            "char_length(text) BETWEEN 1 AND 4096",
            name="telegram_response_outbox_text_check",
        ),
        CheckConstraint(
            "parse_mode IS NULL OR parse_mode = 'HTML'",
            name="telegram_response_outbox_parse_mode_check",
        ),
        CheckConstraint(
            "reply_markup IS NULL OR jsonb_typeof(reply_markup) = 'object'",
            name="telegram_response_outbox_markup_check",
        ),
        Index(
            "ix_telegram_response_outbox_pending",
            "update_id",
            "sequence",
            postgresql_where=text("sent_at IS NULL"),
        ),
    )


class Draft(Base):
    __tablename__ = "drafts"
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid7)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), unique=True)
    state: Mapped[str] = mapped_column(String(30))
    payload: Mapped[dict[str, object]] = mapped_column(JSONB)
    schema_version: Mapped[int] = mapped_column(Integer, default=1, server_default=text("1"))
    revision: Mapped[int] = mapped_column(Integer, default=1, server_default=text("1"))
    suspended: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    presentation_ref: Mapped[str | None] = mapped_column(String(64))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    __table_args__ = (
        CheckConstraint("schema_version >= 1", name="drafts_schema_version_check"),
        CheckConstraint("revision >= 1", name="drafts_revision_check"),
    )


class AuditEvent(Base):
    __tablename__ = "audit_events"
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid7)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    transaction_id: Mapped[UUID | None] = mapped_column(ForeignKey("transactions.id"))
    action: Mapped[str] = mapped_column(String(30))
    data: Mapped[dict[str, object]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    undone_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (Index("ix_audit_events_user_created", "user_id", "created_at"),)


class CategoryRule(Base):
    __tablename__ = "category_rules"
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid7)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    kind: Mapped[str] = mapped_column(String(10))
    pattern: Mapped[str] = mapped_column(String(200))
    normalized_pattern: Mapped[str] = mapped_column(String(200))
    category_id: Mapped[UUID] = mapped_column(ForeignKey("categories.id"))
    account_id: Mapped[UUID | None] = mapped_column(ForeignKey("accounts.id"))
    version: Mapped[int] = mapped_column(Integer, default=1, server_default=text("1"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    __table_args__ = (
        CheckConstraint("kind IN ('expense', 'income')", name="category_rules_kind_check"),
        CheckConstraint("version >= 1", name="category_rules_version_check"),
        CheckConstraint(
            "length(normalized_pattern) > 0",
            name="category_rules_normalized_pattern_check",
        ),
        ForeignKeyConstraint(
            ["category_id", "user_id", "kind"],
            ["categories.id", "categories.user_id", "categories.kind"],
            name="fk_category_rules_category_owner_kind",
        ),
        ForeignKeyConstraint(
            ["account_id", "user_id"],
            ["accounts.id", "accounts.user_id"],
            name="fk_category_rules_account_owner",
        ),
        Index(
            "uq_category_rules_global_pattern",
            "user_id",
            "kind",
            "normalized_pattern",
            unique=True,
            postgresql_where=text("account_id IS NULL"),
        ),
        Index(
            "uq_category_rules_account_pattern",
            "user_id",
            "account_id",
            "kind",
            "normalized_pattern",
            unique=True,
            postgresql_where=text("account_id IS NOT NULL"),
        ),
    )
