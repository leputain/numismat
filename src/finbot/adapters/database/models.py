from datetime import date, datetime, time
from uuid import UUID, uuid7

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy import (
    Time as SqlTime,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from finbot.application.interactions import MAX_PAGE


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
    settings_version: Mapped[int] = mapped_column(
        Integer,
        default=1,
        server_default=text("1"),
    )
    default_account_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    __table_args__ = (
        UniqueConstraint(
            "telegram_user_id",
            "telegram_chat_id",
            name="uq_users_telegram_user_chat",
        ),
        CheckConstraint(
            "telegram_chat_id IS NULL OR telegram_chat_id = telegram_user_id",
            name="users_private_telegram_chat_check",
        ),
        CheckConstraint("settings_version >= 1", name="users_settings_version_check"),
        ForeignKeyConstraint(
            ["default_account_id", "id"],
            ["accounts.id", "accounts.user_id"],
            name="fk_users_default_account_owner",
            use_alter=True,
        ),
    )


class NotificationPreference(Base):
    __tablename__ = "notification_preferences"
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    budget_80_enabled: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default=text("false"),
    )
    budget_100_enabled: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default=text("false"),
    )
    recurring_ready_enabled: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default=text("false"),
    )
    weekly_digest_enabled: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default=text("false"),
    )
    quiet_start: Mapped[time | None] = mapped_column(SqlTime(timezone=False))
    quiet_end: Mapped[time | None] = mapped_column(SqlTime(timezone=False))
    weekly_weekday: Mapped[int] = mapped_column(
        SmallInteger,
        default=0,
        server_default=text("0"),
    )
    weekly_time: Mapped[time] = mapped_column(
        SqlTime(timezone=False),
        default=time(9, 0),
        server_default=text("'09:00:00'"),
    )
    version: Mapped[int] = mapped_column(Integer, default=1, server_default=text("1"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    __table_args__ = (
        CheckConstraint(
            "(quiet_start IS NULL) = (quiet_end IS NULL)",
            name="notification_preferences_quiet_pair_check",
        ),
        CheckConstraint(
            "quiet_start IS NULL OR quiet_start <> quiet_end",
            name="notification_preferences_quiet_range_check",
        ),
        CheckConstraint(
            "EXTRACT(SECOND FROM quiet_start) = 0 AND EXTRACT(SECOND FROM quiet_end) = 0",
            name="notification_preferences_quiet_minute_check",
        ),
        CheckConstraint(
            "weekly_weekday BETWEEN 0 AND 6",
            name="notification_preferences_weekday_check",
        ),
        CheckConstraint(
            "EXTRACT(SECOND FROM weekly_time) = 0",
            name="notification_preferences_weekly_minute_check",
        ),
        CheckConstraint("version >= 1", name="notification_preferences_version_check"),
    )


class NotificationJob(Base):
    __tablename__ = "notification_jobs"
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid7)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(32))
    reference_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    dedupe_digest: Mapped[bytes] = mapped_column(LargeBinary, unique=True)
    status: Mapped[str] = mapped_column(String(16), default="pending", server_default="pending")
    attempt_count: Mapped[int] = mapped_column(
        SmallInteger,
        default=0,
        server_default=text("0"),
    )
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    lease_token: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failure_code: Mapped[str | None] = mapped_column(String(32))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    __table_args__ = (
        CheckConstraint(
            "kind IN ('budget_80', 'budget_100', 'recurring_ready', 'weekly_digest')",
            name="notification_jobs_kind_check",
        ),
        CheckConstraint(
            "(kind = 'weekly_digest' AND reference_id IS NULL) OR "
            "(kind <> 'weekly_digest' AND reference_id IS NOT NULL)",
            name="notification_jobs_reference_check",
        ),
        CheckConstraint(
            "octet_length(dedupe_digest) = 32",
            name="notification_jobs_dedupe_digest_check",
        ),
        CheckConstraint(
            "attempt_count BETWEEN 0 AND 5",
            name="notification_jobs_attempt_count_check",
        ),
        CheckConstraint(
            "failure_code IS NULL OR failure_code IN "
            "('chat_invalid', 'owner_revoked', 'preference_disabled', "
            "'reference_invalid', 'retry_exhausted', 'telegram_rejected', "
            "'telegram_retryable')",
            name="notification_jobs_failure_code_check",
        ),
        CheckConstraint(
            "(lease_token IS NULL) = (lease_until IS NULL)",
            name="notification_jobs_lease_pair_check",
        ),
        CheckConstraint(
            "(status = 'pending' AND lease_token IS NULL AND delivered_at IS NULL "
            "AND attempt_count < 5) OR "
            "(status = 'leased' AND lease_token IS NOT NULL AND delivered_at IS NULL) OR "
            "(status = 'delivered' AND lease_token IS NULL AND delivered_at IS NOT NULL "
            "AND failure_code IS NULL) OR "
            "(status = 'failed' AND lease_token IS NULL AND delivered_at IS NULL "
            "AND failure_code IS NOT NULL)",
            name="notification_jobs_state_check",
        ),
        Index(
            "ix_notification_jobs_pending",
            "available_at",
            "id",
            postgresql_where=text("status = 'pending'"),
        ),
        Index(
            "ix_notification_jobs_lease",
            "lease_until",
            "id",
            postgresql_where=text("status = 'leased'"),
        ),
        Index(
            "ix_notification_jobs_terminal_cleanup",
            "updated_at",
            "id",
            postgresql_where=text("status IN ('delivered', 'failed')"),
        ),
        Index("ix_notification_jobs_owner", "user_id", "created_at", "id"),
    )


class WebSession(Base):
    __tablename__ = "web_sessions"
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid7)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    session_token_hash: Mapped[bytes] = mapped_column(LargeBinary)
    csrf_token_hash: Mapped[bytes] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        UniqueConstraint(
            "session_token_hash",
            name="uq_web_sessions_session_token_hash",
        ),
        UniqueConstraint(
            "csrf_token_hash",
            name="uq_web_sessions_csrf_token_hash",
        ),
        CheckConstraint(
            "octet_length(session_token_hash) = 32",
            name="web_sessions_session_token_hash_check",
        ),
        CheckConstraint(
            "octet_length(csrf_token_hash) = 32",
            name="web_sessions_csrf_token_hash_check",
        ),
        CheckConstraint(
            "session_token_hash <> csrf_token_hash",
            name="web_sessions_distinct_token_hashes_check",
        ),
        CheckConstraint("expires_at > created_at", name="web_sessions_expiry_order_check"),
        CheckConstraint(
            "expires_at <= created_at + INTERVAL '24 hours'",
            name="web_sessions_expiry_bound_check",
        ),
        CheckConstraint(
            "revoked_at IS NULL OR revoked_at >= created_at",
            name="web_sessions_revocation_order_check",
        ),
        Index("ix_web_sessions_expires_at", "expires_at"),
        Index(
            "ix_web_sessions_revoked_at",
            "revoked_at",
            postgresql_where=text("revoked_at IS NOT NULL"),
        ),
        Index(
            "ix_web_sessions_user_active",
            "user_id",
            "expires_at",
            postgresql_where=text("revoked_at IS NULL"),
        ),
    )


class HttpIdempotencyRecord(Base):
    __tablename__ = "http_idempotency"
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid7)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    idempotency_key_hash: Mapped[bytes] = mapped_column(LargeBinary)
    request_fingerprint: Mapped[bytes] = mapped_column(LargeBinary)
    operation: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16))
    http_status: Mapped[int | None] = mapped_column(SmallInteger)
    result_kind: Mapped[str | None] = mapped_column(String(32))
    result_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    result_revision: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "idempotency_key_hash",
            name="uq_http_idempotency_owner_key_hash",
        ),
        CheckConstraint(
            "octet_length(idempotency_key_hash) = 32",
            name="http_idempotency_key_hash_check",
        ),
        CheckConstraint(
            "octet_length(request_fingerprint) = 32",
            name="http_idempotency_request_fingerprint_check",
        ),
        CheckConstraint(
            "operation ~ '^[a-z][a-z0-9_.:-]{0,63}$'",
            name="http_idempotency_operation_check",
        ),
        CheckConstraint(
            "status IN ('in_progress', 'completed')",
            name="http_idempotency_status_check",
        ),
        CheckConstraint(
            "expires_at > created_at",
            name="http_idempotency_expiry_order_check",
        ),
        CheckConstraint(
            "expires_at <= created_at + INTERVAL '24 hours'",
            name="http_idempotency_expiry_bound_check",
        ),
        CheckConstraint(
            "result_kind IS NULL OR result_kind IN "
            "('none', 'draft', 'transaction', 'account', 'category', 'budget', "
            "'recurring_schedule', 'recurring_instance', 'exchange_rate_version', "
            "'bank_import_batch', 'bank_import_row')",
            name="http_idempotency_result_kind_check",
        ),
        CheckConstraint(
            "result_kind IS NULL OR "
            "(result_kind = 'none' AND result_id IS NULL AND result_revision IS NULL) OR "
            "(result_kind <> 'none' AND result_id IS NOT NULL "
            "AND result_revision IS NOT NULL AND result_revision >= 1)",
            name="http_idempotency_result_reference_check",
        ),
        CheckConstraint(
            "(status = 'in_progress' AND completed_at IS NULL AND http_status IS NULL "
            "AND result_kind IS NULL AND result_id IS NULL AND result_revision IS NULL) OR "
            "(status = 'completed' AND completed_at IS NOT NULL "
            "AND completed_at >= created_at AND completed_at <= expires_at "
            "AND http_status IS NOT NULL AND http_status BETWEEN 200 AND 299 "
            "AND result_kind IS NOT NULL)",
            name="http_idempotency_state_check",
        ),
        Index("ix_http_idempotency_expires_at", "expires_at"),
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
    parent_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(Integer, default=1, server_default=text("1"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    __table_args__ = (
        UniqueConstraint("user_id", "kind", "slug"),
        UniqueConstraint("id", "user_id", "kind", name="uq_categories_id_user_kind"),
        ForeignKeyConstraint(
            ["parent_id", "user_id", "kind"],
            ["categories.id", "categories.user_id", "categories.kind"],
            name="fk_categories_parent_owner_kind",
        ),
        CheckConstraint("kind IN ('expense', 'income')", name="categories_kind_check"),
        CheckConstraint("version >= 1", name="categories_version_check"),
    )


class Budget(Base):
    __tablename__ = "budgets"
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid7)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(60))
    limit_minor: Mapped[int] = mapped_column(BigInteger)
    currency: Mapped[str] = mapped_column(String(3))
    category_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    category_kind: Mapped[str] = mapped_column(
        String(10), default="expense", server_default=text("'expense'")
    )
    starts_on: Mapped[date] = mapped_column(Date)
    ends_on: Mapped[date] = mapped_column(Date)
    timezone: Mapped[str] = mapped_column(String(64))
    version: Mapped[int] = mapped_column(Integer, default=1, server_default=text("1"))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    __table_args__ = (
        CheckConstraint(
            "name = btrim(name) AND char_length(name) BETWEEN 1 AND 60 AND name !~ '[[:cntrl:]]'",
            name="budgets_name_check",
        ),
        CheckConstraint(
            "limit_minor BETWEEN 1 AND 9223372036854775807",
            name="budgets_limit_minor_check",
        ),
        CheckConstraint("currency ~ '^[A-Z]{3}$'", name="budgets_currency_check"),
        CheckConstraint("category_kind = 'expense'", name="budgets_category_kind_check"),
        CheckConstraint(
            "ends_on >= starts_on AND ends_on < starts_on + 366",
            name="budgets_period_check",
        ),
        CheckConstraint(
            "timezone = btrim(timezone) AND char_length(timezone) BETWEEN 1 AND 64",
            name="budgets_timezone_check",
        ),
        CheckConstraint("version >= 1", name="budgets_version_check"),
        CheckConstraint(
            "deleted_at IS NULL OR deleted_at >= created_at",
            name="budgets_deletion_order_check",
        ),
        ForeignKeyConstraint(
            ["category_id", "user_id", "category_kind"],
            ["categories.id", "categories.user_id", "categories.kind"],
            name="fk_budgets_category_owner_kind",
        ),
        Index(
            "ix_budgets_user_period_active",
            "user_id",
            "starts_on",
            "id",
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index(
            "ix_budgets_user_period_deleted",
            "user_id",
            "starts_on",
            "id",
            postgresql_where=text("deleted_at IS NOT NULL"),
        ),
    )


class RecurringSchedule(Base):
    __tablename__ = "recurring_schedules"
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid7)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(60))
    type: Mapped[str] = mapped_column(String(10))
    amount_minor: Mapped[int] = mapped_column(BigInteger)
    currency: Mapped[str] = mapped_column(String(3))
    account_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True))
    category_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True))
    cadence: Mapped[str] = mapped_column(String(10))
    interval: Mapped[int] = mapped_column(SmallInteger)
    anchor_date: Mapped[date] = mapped_column(Date)
    local_time: Mapped[time] = mapped_column(SqlTime(timezone=False))
    timezone: Mapped[str] = mapped_column(String(64))
    ends_on: Mapped[date | None] = mapped_column(Date)
    description: Mapped[str] = mapped_column(String(500), default="")
    next_occurrence_index: Mapped[int] = mapped_column(Integer, default=0)
    next_due_local: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    next_due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    paused_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    pause_reason: Mapped[str | None] = mapped_column(String(32))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(Integer, default=1, server_default=text("1"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    __table_args__ = (
        UniqueConstraint("id", "user_id", name="uq_recurring_schedules_id_user_id"),
        CheckConstraint(
            "name = btrim(name) AND char_length(name) BETWEEN 1 AND 60 AND name !~ '[[:cntrl:]]'",
            name="recurring_schedules_name_check",
        ),
        CheckConstraint("type IN ('expense', 'income')", name="recurring_schedules_type_check"),
        CheckConstraint(
            "amount_minor BETWEEN 1 AND 9223372036854775807",
            name="recurring_schedules_amount_check",
        ),
        CheckConstraint(
            "currency ~ '^[A-Z]{3}$'",
            name="recurring_schedules_currency_check",
        ),
        CheckConstraint(
            "(cadence = 'daily' AND interval BETWEEN 1 AND 365) OR "
            "(cadence = 'weekly' AND interval BETWEEN 1 AND 52) OR "
            "(cadence = 'monthly' AND interval BETWEEN 1 AND 24)",
            name="recurring_schedules_rule_check",
        ),
        CheckConstraint(
            "EXTRACT(SECOND FROM local_time) = 0",
            name="recurring_schedules_minute_precision_check",
        ),
        CheckConstraint(
            "timezone = btrim(timezone) AND char_length(timezone) BETWEEN 1 AND 64",
            name="recurring_schedules_timezone_check",
        ),
        CheckConstraint(
            "ends_on IS NULL OR ends_on >= anchor_date",
            name="recurring_schedules_end_check",
        ),
        CheckConstraint(
            "char_length(description) <= 500 AND description !~ '[[:cntrl:]]'",
            name="recurring_schedules_description_check",
        ),
        CheckConstraint(
            "next_occurrence_index >= 0",
            name="recurring_schedules_occurrence_index_check",
        ),
        CheckConstraint(
            "(next_due_local IS NULL) = (next_due_at IS NULL)",
            name="recurring_schedules_due_pair_check",
        ),
        CheckConstraint(
            "pause_reason IS NULL OR (paused_at IS NOT NULL AND pause_reason IN "
            "('account_unavailable', 'category_unavailable', 'currency_mismatch', "
            "'schedule_invalid'))",
            name="recurring_schedules_pause_check",
        ),
        CheckConstraint("version >= 1", name="recurring_schedules_version_check"),
        CheckConstraint(
            "deleted_at IS NULL OR deleted_at >= created_at",
            name="recurring_schedules_deletion_order_check",
        ),
        ForeignKeyConstraint(
            ["account_id", "user_id"],
            ["accounts.id", "accounts.user_id"],
            name="fk_recurring_schedules_account_owner",
        ),
        ForeignKeyConstraint(
            ["category_id", "user_id", "type"],
            ["categories.id", "categories.user_id", "categories.kind"],
            name="fk_recurring_schedules_category_owner_kind",
        ),
        Index(
            "ix_recurring_schedules_due_active",
            "next_due_at",
            "id",
            postgresql_where=text(
                "deleted_at IS NULL AND paused_at IS NULL AND next_due_at IS NOT NULL"
            ),
        ),
        Index(
            "ix_recurring_schedules_user_active",
            "user_id",
            "created_at",
            "id",
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index(
            "ix_recurring_schedules_user_deleted",
            "user_id",
            "created_at",
            "id",
            postgresql_where=text("deleted_at IS NOT NULL"),
        ),
    )


class RecurringInstance(Base):
    __tablename__ = "recurring_instances"
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid7)
    schedule_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True))
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    occurrence_index: Mapped[int] = mapped_column(Integer)
    nominal_local: Mapped[datetime] = mapped_column(DateTime(timezone=False))
    scheduled_for: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    timezone: Mapped[str] = mapped_column(String(64))
    dst_adjusted: Mapped[bool] = mapped_column(Boolean, default=False)
    type: Mapped[str] = mapped_column(String(10))
    amount_minor: Mapped[int] = mapped_column(BigInteger)
    currency: Mapped[str] = mapped_column(String(3))
    account_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True))
    category_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True))
    description: Mapped[str] = mapped_column(String(500), default="")
    status: Mapped[str] = mapped_column(String(16), default="pending")
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    attempt_count: Mapped[int] = mapped_column(SmallInteger, default=0)
    failure_code: Mapped[str | None] = mapped_column(String(32))
    draft_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    generated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    skipped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(Integer, default=1, server_default=text("1"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    __table_args__ = (
        UniqueConstraint(
            "schedule_id",
            "occurrence_index",
            name="uq_recurring_instances_schedule_occurrence",
        ),
        UniqueConstraint("id", "user_id", name="uq_recurring_instances_id_user_id"),
        UniqueConstraint("draft_id", name="uq_recurring_instances_draft_id"),
        CheckConstraint(
            "occurrence_index >= 0",
            name="recurring_instances_occurrence_index_check",
        ),
        CheckConstraint(
            "timezone = btrim(timezone) AND char_length(timezone) BETWEEN 1 AND 64",
            name="recurring_instances_timezone_check",
        ),
        CheckConstraint("type IN ('expense', 'income')", name="recurring_instances_type_check"),
        CheckConstraint(
            "amount_minor BETWEEN 1 AND 9223372036854775807",
            name="recurring_instances_amount_check",
        ),
        CheckConstraint(
            "currency ~ '^[A-Z]{3}$'",
            name="recurring_instances_currency_check",
        ),
        CheckConstraint(
            "char_length(description) <= 500 AND description !~ '[[:cntrl:]]'",
            name="recurring_instances_description_check",
        ),
        CheckConstraint(
            "attempt_count BETWEEN 0 AND 32767",
            name="recurring_instances_attempt_count_check",
        ),
        CheckConstraint(
            "failure_code IS NULL OR failure_code IN "
            "('account_unavailable', 'category_unavailable', 'currency_mismatch', "
            "'schedule_invalid')",
            name="recurring_instances_failure_code_check",
        ),
        CheckConstraint(
            "(status = 'pending' AND generated_at IS NULL AND skipped_at IS NULL "
            "AND failure_code IS NULL AND draft_id IS NULL) OR "
            "(status = 'generated' AND generated_at IS NOT NULL AND skipped_at IS NULL "
            "AND failure_code IS NULL) OR "
            "(status = 'blocked' AND generated_at IS NULL AND skipped_at IS NULL "
            "AND failure_code IS NOT NULL AND draft_id IS NULL) OR "
            "(status = 'skipped' AND generated_at IS NULL AND skipped_at IS NOT NULL "
            "AND failure_code IS NULL AND draft_id IS NULL)",
            name="recurring_instances_state_check",
        ),
        CheckConstraint("version >= 1", name="recurring_instances_version_check"),
        ForeignKeyConstraint(
            ["schedule_id", "user_id"],
            ["recurring_schedules.id", "recurring_schedules.user_id"],
            name="fk_recurring_instances_schedule_owner",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["account_id", "user_id"],
            ["accounts.id", "accounts.user_id"],
            name="fk_recurring_instances_account_owner",
        ),
        ForeignKeyConstraint(
            ["category_id", "user_id", "type"],
            ["categories.id", "categories.user_id", "categories.kind"],
            name="fk_recurring_instances_category_owner_kind",
        ),
        ForeignKeyConstraint(
            ["draft_id", "user_id"],
            ["drafts.id", "drafts.user_id"],
            name="fk_recurring_instances_draft_owner",
        ),
        Index(
            "ix_recurring_instances_pending_due",
            "next_attempt_at",
            "user_id",
            "scheduled_for",
            "id",
            postgresql_where=text("status = 'pending'"),
        ),
        Index(
            "ix_recurring_instances_schedule_page",
            "user_id",
            "schedule_id",
            "scheduled_for",
            "id",
        ),
    )


class ExchangeRateSource(Base):
    __tablename__ = "exchange_rate_sources"
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid7)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(16))
    target_currency: Mapped[str] = mapped_column(String(3))
    latest_version: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "kind",
            "target_currency",
            name="uq_exchange_rate_sources_owner_kind_target",
        ),
        UniqueConstraint(
            "id",
            "user_id",
            "target_currency",
            name="uq_exchange_rate_sources_id_owner_target",
        ),
        CheckConstraint("kind = 'manual'", name="exchange_rate_sources_kind_check"),
        CheckConstraint(
            "target_currency ~ '^[A-Z]{3}$'",
            name="exchange_rate_sources_target_currency_check",
        ),
        CheckConstraint(
            "latest_version BETWEEN 1 AND 2147483647",
            name="exchange_rate_sources_latest_version_check",
        ),
        Index(
            "ix_exchange_rate_sources_owner_target",
            "user_id",
            "target_currency",
            "id",
        ),
    )


class ExchangeRateVersion(Base):
    __tablename__ = "exchange_rate_versions"
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid7)
    source_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True))
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    target_currency: Mapped[str] = mapped_column(String(3))
    version: Mapped[int] = mapped_column(Integer)
    effective_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (
        UniqueConstraint(
            "source_id",
            "version",
            name="uq_exchange_rate_versions_source_version",
        ),
        UniqueConstraint(
            "id",
            "target_currency",
            name="uq_exchange_rate_versions_id_target",
        ),
        CheckConstraint(
            "target_currency ~ '^[A-Z]{3}$'",
            name="exchange_rate_versions_target_currency_check",
        ),
        CheckConstraint(
            "version BETWEEN 1 AND 2147483647",
            name="exchange_rate_versions_version_check",
        ),
        ForeignKeyConstraint(
            ["source_id", "user_id", "target_currency"],
            [
                "exchange_rate_sources.id",
                "exchange_rate_sources.user_id",
                "exchange_rate_sources.target_currency",
            ],
            name="fk_exchange_rate_versions_source_owner_target",
            ondelete="CASCADE",
        ),
        Index(
            "ix_exchange_rate_versions_source_page",
            "source_id",
            "created_at",
            "id",
        ),
    )


class ExchangeRateEntryRecord(Base):
    __tablename__ = "exchange_rate_entries"
    rate_version_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        primary_key=True,
    )
    source_currency: Mapped[str] = mapped_column(String(3), primary_key=True)
    target_currency: Mapped[str] = mapped_column(String(3))
    coefficient: Mapped[int] = mapped_column(BigInteger)
    scale: Mapped[int] = mapped_column(SmallInteger)
    source_minor_digits: Mapped[int] = mapped_column(SmallInteger, default=2)
    target_minor_digits: Mapped[int] = mapped_column(SmallInteger, default=2)
    __table_args__ = (
        CheckConstraint(
            "source_currency ~ '^[A-Z]{3}$'",
            name="exchange_rate_entries_source_currency_check",
        ),
        CheckConstraint(
            "target_currency ~ '^[A-Z]{3}$'",
            name="exchange_rate_entries_target_currency_check",
        ),
        CheckConstraint(
            "source_currency <> target_currency",
            name="exchange_rate_entries_non_identity_check",
        ),
        CheckConstraint(
            "coefficient BETWEEN 1 AND 9223372036854775807",
            name="exchange_rate_entries_coefficient_check",
        ),
        CheckConstraint(
            "scale BETWEEN 0 AND 12",
            name="exchange_rate_entries_scale_check",
        ),
        CheckConstraint(
            "char_length(coefficient::text) <= 6 + scale",
            name="exchange_rate_entries_integer_digits_check",
        ),
        CheckConstraint(
            "scale = 0 OR coefficient % 10 <> 0",
            name="exchange_rate_entries_canonical_check",
        ),
        CheckConstraint(
            "source_minor_digits = 2 AND target_minor_digits = 2",
            name="exchange_rate_entries_minor_digits_check",
        ),
        ForeignKeyConstraint(
            ["rate_version_id", "target_currency"],
            ["exchange_rate_versions.id", "exchange_rate_versions.target_currency"],
            name="fk_exchange_rate_entries_version_target",
            ondelete="CASCADE",
        ),
    )


class ImportBatch(Base):
    __tablename__ = "import_batches"
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid7)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    account_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True))
    profile: Mapped[str] = mapped_column(String(32))
    encoding: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16), default="open", server_default=text("'open'"))
    row_count: Mapped[int] = mapped_column(SmallInteger)
    version: Mapped[int] = mapped_column(Integer, default=1, server_default=text("1"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        UniqueConstraint("id", "user_id", name="uq_import_batches_id_user_id"),
        ForeignKeyConstraint(
            ["account_id", "user_id"],
            ["accounts.id", "accounts.user_id"],
            name="fk_import_batches_account_owner",
        ),
        CheckConstraint("profile = 'canonical_v1'", name="import_batches_profile_check"),
        CheckConstraint(
            "encoding IN ('utf-8', 'windows-1251')",
            name="import_batches_encoding_check",
        ),
        CheckConstraint(
            "status IN ('open', 'completed', 'cancelled')",
            name="import_batches_status_check",
        ),
        CheckConstraint(
            "row_count BETWEEN 1 AND 2000",
            name="import_batches_row_count_check",
        ),
        CheckConstraint(
            "version BETWEEN 1 AND 2147483647",
            name="import_batches_version_check",
        ),
        CheckConstraint(
            "updated_at >= created_at AND "
            "(completed_at IS NULL OR completed_at >= created_at) AND "
            "(cancelled_at IS NULL OR cancelled_at >= created_at)",
            name="import_batches_timestamp_order_check",
        ),
        CheckConstraint(
            "(status = 'open' AND completed_at IS NULL AND cancelled_at IS NULL) OR "
            "(status = 'completed' AND completed_at IS NOT NULL "
            "AND cancelled_at IS NULL) OR "
            "(status = 'cancelled' AND cancelled_at IS NOT NULL "
            "AND completed_at IS NULL)",
            name="import_batches_state_check",
        ),
        Index("ix_import_batches_owner_page", "user_id", "created_at", "id"),
        Index(
            "ix_import_batches_owner_status_page",
            "user_id",
            "status",
            "created_at",
            "id",
        ),
    )


class ImportRow(Base):
    __tablename__ = "import_rows"
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid7)
    batch_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True))
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    position: Mapped[int] = mapped_column(SmallInteger)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    type: Mapped[str] = mapped_column(String(10))
    amount_minor: Mapped[int] = mapped_column(BigInteger)
    currency: Mapped[str] = mapped_column(String(3))
    description: Mapped[str] = mapped_column(String(500), default="")
    fingerprint: Mapped[bytes] = mapped_column(LargeBinary)
    reference_digest: Mapped[bytes | None] = mapped_column(LargeBinary)
    status: Mapped[str] = mapped_column(
        String(16),
        default="pending",
        server_default=text("'pending'"),
    )
    draft_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(Integer, default=1, server_default=text("1"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    __table_args__ = (
        UniqueConstraint("batch_id", "position", name="uq_import_rows_batch_position"),
        UniqueConstraint("id", "user_id", name="uq_import_rows_id_user_id"),
        UniqueConstraint("draft_id", name="uq_import_rows_draft_id"),
        ForeignKeyConstraint(
            ["batch_id", "user_id"],
            ["import_batches.id", "import_batches.user_id"],
            name="fk_import_rows_batch_owner",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["draft_id", "user_id"],
            ["drafts.id", "drafts.user_id"],
            name="fk_import_rows_draft_owner",
        ),
        CheckConstraint("position BETWEEN 1 AND 2000", name="import_rows_position_check"),
        CheckConstraint("type IN ('expense', 'income')", name="import_rows_type_check"),
        CheckConstraint(
            "amount_minor BETWEEN 1 AND 9223372036854775807",
            name="import_rows_amount_check",
        ),
        CheckConstraint("currency ~ '^[A-Z]{3}$'", name="import_rows_currency_check"),
        CheckConstraint(
            "char_length(description) <= 500 AND description !~ '[[:cntrl:]]'",
            name="import_rows_description_check",
        ),
        CheckConstraint(
            "octet_length(fingerprint) = 32",
            name="import_rows_fingerprint_check",
        ),
        CheckConstraint(
            "reference_digest IS NULL OR octet_length(reference_digest) = 32",
            name="import_rows_reference_digest_check",
        ),
        CheckConstraint(
            "status IN ('pending', 'staged', 'confirmed', 'linked', 'skipped', 'cancelled')",
            name="import_rows_status_check",
        ),
        CheckConstraint(
            "version BETWEEN 1 AND 2147483647",
            name="import_rows_version_check",
        ),
        CheckConstraint(
            "updated_at >= created_at AND (resolved_at IS NULL OR resolved_at >= created_at)",
            name="import_rows_timestamp_order_check",
        ),
        CheckConstraint(
            "(status = 'pending' AND draft_id IS NULL AND resolved_at IS NULL) OR "
            "(status = 'staged' AND resolved_at IS NULL) OR "
            "(status IN ('confirmed', 'linked', 'skipped', 'cancelled') "
            "AND draft_id IS NULL AND resolved_at IS NOT NULL)",
            name="import_rows_state_check",
        ),
        Index(
            "ix_import_rows_owner_batch_page",
            "user_id",
            "batch_id",
            "position",
            "id",
        ),
        Index(
            "ix_import_rows_owner_batch_status_page",
            "user_id",
            "batch_id",
            "status",
            "position",
            "id",
        ),
        Index("ix_import_rows_owner_fingerprint", "user_id", "fingerprint", "id"),
        Index(
            "ix_import_rows_owner_reference_digest",
            "user_id",
            "reference_digest",
            "id",
            postgresql_where=text("reference_digest IS NOT NULL"),
        ),
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
    recurring_instance_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    import_row_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(Integer, default=1)
    __table_args__ = (
        UniqueConstraint("id", "user_id", name="uq_transactions_id_user_id"),
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
        ForeignKeyConstraint(
            ["recurring_instance_id", "user_id"],
            ["recurring_instances.id", "recurring_instances.user_id"],
            name="fk_transactions_recurring_instance_owner",
        ),
        ForeignKeyConstraint(
            ["import_row_id", "user_id"],
            ["import_rows.id", "import_rows.user_id"],
            name="fk_transactions_import_row_owner",
        ),
        UniqueConstraint(
            "recurring_instance_id",
            name="uq_transactions_recurring_instance_id",
        ),
        UniqueConstraint("import_row_id", name="uq_transactions_import_row_id"),
        CheckConstraint(
            "source IN ('manual', 'recurring', 'bank_import')",
            name="transactions_source_check",
        ),
        CheckConstraint(
            "(recurring_instance_id IS NULL AND source <> 'recurring') OR "
            "(recurring_instance_id IS NOT NULL AND source = 'recurring')",
            name="transactions_recurring_source_check",
        ),
        CheckConstraint(
            "(import_row_id IS NULL AND source <> 'bank_import') OR "
            "(import_row_id IS NOT NULL AND source = 'bank_import')",
            name="transactions_import_source_check",
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
    draft_revision: Mapped[int | None] = mapped_column(Integer)
    history_page: Mapped[int | None] = mapped_column(Integer)
    pending_history_page: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        UniqueConstraint(
            "update_id", "sequence", name="uq_telegram_response_outbox_update_sequence"
        ),
        ForeignKeyConstraint(
            ["owner_telegram_user_id", "chat_id"],
            ["users.telegram_user_id", "users.telegram_chat_id"],
            name="fk_telegram_response_outbox_user_chat",
        ),
        CheckConstraint("sequence >= 0", name="telegram_response_outbox_sequence_check"),
        CheckConstraint(
            "method IN ('send_message', 'edit_message_text', 'send_csv_export')",
            name="telegram_response_outbox_method_check",
        ),
        CheckConstraint(
            "(method = 'send_message' AND message_id IS NULL) "
            "OR (method = 'edit_message_text' AND message_id > 0) "
            "OR (method = 'send_csv_export' AND message_id IS NULL)",
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
        CheckConstraint(
            "method <> 'send_csv_export' OR ("
            "text = 'csv_export:v1' AND parse_mode IS NULL AND reply_markup IS NULL "
            "AND draft_id IS NULL AND draft_revision IS NULL "
            "AND history_page IS NULL AND pending_history_page IS NULL)",
            name="telegram_response_outbox_csv_export_job_check",
        ),
        CheckConstraint(
            "draft_revision IS NULL OR draft_revision >= 1",
            name="telegram_response_outbox_draft_revision_check",
        ),
        CheckConstraint(
            f"history_page IS NULL OR history_page BETWEEN 0 AND {MAX_PAGE}",
            name="telegram_response_outbox_history_page_check",
        ),
        CheckConstraint(
            f"pending_history_page IS NULL OR pending_history_page BETWEEN 0 AND {MAX_PAGE}",
            name="telegram_response_outbox_pending_history_page_check",
        ),
        CheckConstraint(
            "(history_page IS NULL AND pending_history_page IS NULL) OR draft_revision IS NOT NULL",
            name="telegram_response_outbox_presentation_context_check",
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
        UniqueConstraint("id", "user_id", name="uq_drafts_id_user_id"),
        CheckConstraint("schema_version >= 1", name="drafts_schema_version_check"),
        CheckConstraint("revision >= 1", name="drafts_revision_check"),
    )


class TelegramDraftPresentation(Base):
    """Telegram-only projection of the message currently presenting a draft."""

    __tablename__ = "telegram_draft_presentations"
    draft_id: Mapped[UUID] = mapped_column(
        ForeignKey("drafts.id", ondelete="CASCADE"), primary_key=True
    )
    chat_id: Mapped[int] = mapped_column(BigInteger)
    message_id: Mapped[int] = mapped_column(BigInteger)
    rendered_revision: Mapped[int] = mapped_column(Integer)
    history_page: Mapped[int | None] = mapped_column(Integer)
    pending_history_page: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    __table_args__ = (
        UniqueConstraint(
            "chat_id", "message_id", name="uq_telegram_draft_presentations_chat_message"
        ),
        CheckConstraint("chat_id <> 0", name="telegram_draft_presentations_chat_check"),
        CheckConstraint("message_id > 0", name="telegram_draft_presentations_message_check"),
        CheckConstraint(
            "rendered_revision >= 1",
            name="telegram_draft_presentations_revision_check",
        ),
        CheckConstraint(
            f"history_page IS NULL OR history_page BETWEEN 0 AND {MAX_PAGE}",
            name="telegram_draft_presentations_history_page_check",
        ),
        CheckConstraint(
            f"pending_history_page IS NULL OR pending_history_page BETWEEN 0 AND {MAX_PAGE}",
            name="telegram_draft_presentations_pending_history_page_check",
        ),
    )


class AuditEvent(Base):
    __tablename__ = "audit_events"
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid7)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    transaction_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    action: Mapped[str] = mapped_column(String(30))
    data: Mapped[dict[str, object]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    undone_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (
        ForeignKeyConstraint(
            ["transaction_id", "user_id"],
            ["transactions.id", "transactions.user_id"],
            name="fk_audit_events_transaction_owner",
        ),
        Index("ix_audit_events_user_created", "user_id", "created_at"),
    )


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
