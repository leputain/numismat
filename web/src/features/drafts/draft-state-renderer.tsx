import { useState } from "react";

import type { Draft, DraftAction } from "../../shared/api/types";
import { useSessionFormat } from "../../shared/auth/use-session-format";
import { dateInputToWire, formatTransactionDate } from "../../shared/format/date-time";
import { formatMinorAmount, formatTransactionMoney } from "../../shared/finance/money";
import { AccountSelector, CategorySelector } from "../catalogs/catalog-selectors";

interface DraftStateRendererProps {
  readonly draft: Draft;
  readonly disabled: boolean;
  onAction(action: DraftAction, catalog?: "accounts" | "categories"): void;
  onConfirm(): void;
  onCloseTelegram(): void;
}

function StepIntro({ title, text }: { readonly title: string; readonly text: string }) {
  return (
    <header className="draft-step__heading">
      <p className="eyebrow">Следующий шаг</p>
      <h2 className="section-title">{title}</h2>
      <p className="page-description">{text}</p>
    </header>
  );
}

interface TextEntryProps {
  readonly disabled: boolean;
  readonly label: string;
  readonly placeholder: string;
  readonly submitLabel?: string;
  readonly multiline?: boolean;
  readonly inputMode?: "decimal" | "text";
  onSubmit(value: string): void;
}

function TextEntry({
  disabled,
  label,
  placeholder,
  submitLabel = "Продолжить",
  multiline = false,
  inputMode = "text",
  onSubmit,
}: TextEntryProps) {
  const [value, setValue] = useState("");
  const control = multiline ? (
    <textarea
      autoComplete="off"
      className="field-input field-input--textarea"
      disabled={disabled}
      id="draft-value"
      onChange={(event) => setValue(event.currentTarget.value)}
      placeholder={placeholder}
      rows={4}
      spellCheck={false}
      value={value}
    />
  ) : (
    <input
      autoComplete="off"
      className="field-input"
      disabled={disabled}
      id="draft-value"
      inputMode={inputMode}
      onChange={(event) => setValue(event.currentTarget.value)}
      placeholder={placeholder}
      spellCheck={false}
      type="text"
      value={value}
    />
  );
  return (
    <form
      className="field-stack"
      onSubmit={(event) => {
        event.preventDefault();
        if (value.trim().length > 0) {
          onSubmit(value);
        }
      }}
    >
      <label className="field-label" htmlFor="draft-value">{label}</label>
      {control}
      <button className="button button--primary" disabled={disabled || value.trim().length === 0} type="submit">
        {submitLabel}
      </button>
    </form>
  );
}

function DateEntry({ disabled, onSubmit }: { readonly disabled: boolean; onSubmit(value: string): void }) {
  const [value, setValue] = useState("");
  return (
    <form
      className="field-stack"
      onSubmit={(event) => {
        event.preventDefault();
        const wire = dateInputToWire(value);
        if (wire !== undefined) {
          onSubmit(wire);
        }
      }}
    >
      <label className="field-label" htmlFor="draft-date">Дата операции</label>
      <input
        autoComplete="off"
        className="field-input"
        disabled={disabled}
        id="draft-date"
        onChange={(event) => setValue(event.currentTarget.value)}
        required
        type="date"
        value={value}
      />
      <button className="button button--primary" disabled={disabled || value.length === 0} type="submit">Сохранить дату</button>
    </form>
  );
}

function TypeSelector({
  disabled,
  onSelect,
}: {
  readonly disabled: boolean;
  onSelect(value: "expense" | "income"): void;
}) {
  return (
    <div className="choice-grid">
      <button className="choice-card choice-card--expense" disabled={disabled} onClick={() => onSelect("expense")} type="button">
        <span aria-hidden="true">↘</span><strong>Расход</strong><small>Деньги списаны со счёта</small>
      </button>
      <button className="choice-card choice-card--income" disabled={disabled} onClick={() => onSelect("income")} type="button">
        <span aria-hidden="true">↗</span><strong>Доход</strong><small>Деньги поступили на счёт</small>
      </button>
    </div>
  );
}

function DatePresetSelector({
  disabled,
  onSelect,
}: {
  readonly disabled: boolean;
  onSelect(value: "custom" | "today" | "yesterday"): void;
}) {
  return (
    <div className="choice-list">
      <button className="choice-row" disabled={disabled} onClick={() => onSelect("today")} type="button">Сегодня <span aria-hidden="true">›</span></button>
      <button className="choice-row" disabled={disabled} onClick={() => onSelect("yesterday")} type="button">Вчера <span aria-hidden="true">›</span></button>
      <button className="choice-row" disabled={disabled} onClick={() => onSelect("custom")} type="button">Другая дата <span aria-hidden="true">›</span></button>
    </div>
  );
}

function DraftReview({ draft }: { readonly draft: Draft }) {
  const { locale, timeZone } = useSessionFormat();
  const transaction = draft.transaction;
  const formattedMoney =
    transaction?.amount_minor != null && transaction.currency != null && transaction.type != null
      ? formatTransactionMoney(
          transaction.amount_minor,
          transaction.currency,
          transaction.type,
          locale,
        )
      : "Сумма не указана";
  return (
    <article className="draft-review">
      <div className="draft-review__hero">
        <span aria-hidden="true" className="draft-review__emoji">{transaction?.category?.emoji ?? "○"}</span>
        <div>
          <p className="draft-review__category">{transaction?.category?.name ?? "Категория не выбрана"}</p>
          <p className={`draft-review__amount draft-review__amount--${transaction?.type ?? "neutral"}`}>
            {formattedMoney}
          </p>
        </div>
      </div>
      <dl className="detail-list detail-list--compact">
        <div><dt>Тип</dt><dd>{transaction?.type === "income" ? "Доход" : transaction?.type === "expense" ? "Расход" : "—"}</dd></div>
        <div><dt>Счёт</dt><dd>{transaction?.account?.name ?? "—"}</dd></div>
        <div><dt>Дата</dt><dd>{transaction?.occurred_at == null ? "—" : formatTransactionDate(transaction.occurred_at, locale, timeZone)}</dd></div>
        <div><dt>Описание</dt><dd>{transaction?.description || "Без описания"}</dd></div>
      </dl>
    </article>
  );
}

function ReviewActions({
  disabled,
  onAction,
  restricted = false,
}: {
  readonly disabled: boolean;
  readonly restricted?: boolean;
  onAction(action: "edit_account" | "edit_amount" | "edit_category" | "edit_date" | "edit_description" | "edit_type"): void;
}) {
  const actions = restricted ? ([
    ["edit_category", "Категория"],
    ["edit_description", "Описание"],
  ] as const) : ([
    ["edit_type", "Тип"],
    ["edit_amount", "Сумма"],
    ["edit_category", "Категория"],
    ["edit_account", "Счёт"],
    ["edit_date", "Дата"],
    ["edit_description", "Описание"],
  ] as const);
  return (
    <div className="edit-grid">
      {actions.map(([action, label]) => (
        <button className="button button--ghost" disabled={disabled} key={action} onClick={() => onAction(action)} type="button">{label}</button>
      ))}
    </div>
  );
}

function RuleControls({
  draft,
  disabled,
  onAction,
}: {
  readonly draft: Draft;
  readonly disabled: boolean;
  onAction(value: "account" | "global" | "remove"): void;
}) {
  if (draft.rule?.offered !== true) {
    return null;
  }
  return (
    <fieldset className="rule-box">
      <legend>Запомнить выбор категории</legend>
      <p>Правило применяется только после вашего явного выбора.</p>
      <div className="rule-box__actions">
        <button aria-pressed={draft.rule.selected_scope === "global"} className="button button--ghost" disabled={disabled} onClick={() => onAction("global")} type="button">Для всех счетов</button>
        <button aria-pressed={draft.rule.selected_scope === "account"} className="button button--ghost" disabled={disabled} onClick={() => onAction("account")} type="button">Только для счёта</button>
        <button className="button button--ghost" disabled={disabled} onClick={() => onAction("remove")} type="button">Не запоминать</button>
      </div>
    </fieldset>
  );
}

function EditTypeStep({
  draft,
  disabled,
  onAction,
}: {
  readonly draft: Draft;
  readonly disabled: boolean;
  onAction(action: DraftAction): void;
}) {
  const [type, setType] = useState<"expense" | "income">(draft.transaction?.type ?? "expense");
  return (
    <div className="space-y-5">
      <StepIntro title="Изменить тип" text="Новый тип и подходящая категория сохранятся вместе." />
      <div aria-label="Тип операции" className="segment-control">
        <button aria-pressed={type === "expense"} className="segment-control__button" disabled={disabled} onClick={() => setType("expense")} type="button">Расход</button>
        <button aria-pressed={type === "income"} className="segment-control__button" disabled={disabled} onClick={() => setType("income")} type="button">Доход</button>
      </div>
      <CategorySelector
        allowCustom={false}
        disabled={disabled}
        kind={type}
        onCustom={() => undefined}
        onSelect={(category) =>
          onAction({
            action: "select_edit_type",
            revision: draft.revision,
            value: type,
            category: { kind: "existing", id: category.id, version: category.version },
          })
        }
      />
    </div>
  );
}

export function DraftStateRenderer({
  draft,
  disabled,
  onAction,
  onConfirm,
  onCloseTelegram,
}: DraftStateRendererProps) {
  const { locale } = useSessionFormat();
  const inputText = (text: string, catalog?: "accounts" | "categories") =>
    onAction({ action: "input_text", revision: draft.revision, text }, catalog);
  const navigate = (
    action: "back" | "edit_account" | "edit_amount" | "edit_category" | "edit_date" | "edit_description" | "edit_type" | "skip_description",
  ) => onAction({ action, revision: draft.revision });
  const type = draft.transaction?.type;

  switch (draft.state) {
    case "wizard_type": {
      const amountMinor = draft.flow === "quick" ? draft.transaction?.amount_minor : null;
      return (
        <div className="space-y-5">
          <StepIntro
            title="Расход или доход?"
            text={
              amountMinor == null
                ? "Выберите направление движения денег."
                : "Сумма уже распознана. Осталось выбрать направление движения денег."
            }
          />
          {amountMinor == null ? null : (
            <p aria-label={`Распознанная сумма ${formatMinorAmount(amountMinor, locale)}`} className="captured-amount">
              <span>Распознанная сумма</span>
              <strong>{formatMinorAmount(amountMinor, locale)}</strong>
            </p>
          )}
          <TypeSelector disabled={disabled} onSelect={(value) => onAction({ action: "select_type", revision: draft.revision, value })} />
        </div>
      );
    }
    case "review_type":
      return (
        <div className="space-y-5">
          <StepIntro title="Тип операции" text="Выберите направление движения денег." />
          <TypeSelector disabled={disabled} onSelect={(value) => onAction({ action: "select_type", revision: draft.revision, value })} />
        </div>
      );
    case "wizard_amount":
    case "review_amount":
    case "edit_amount":
      return (
        <div className="space-y-5">
          <StepIntro title="Сумма" text="Введите сумму в валюте выбранного счёта. Перед продолжением мы проверим формат." />
          <TextEntry disabled={disabled} inputMode="decimal" label="Сумма" onSubmit={inputText} placeholder="1 250,50" />
        </div>
      );
    case "wizard_category":
    case "quick_category":
    case "category_required":
    case "review_category":
    case "edit_category":
      if (type == null) {
        return <UnsupportedState onClose={onCloseTelegram} />;
      }
      return (
        <div className="space-y-5">
          <StepIntro title="Категория" text="Начните вводить название или выберите категорию из списка." />
          <CategorySelector
            allowCustom={draft.state !== "edit_category" && draft.flow !== "bank_import"}
            disabled={disabled}
            kind={type}
            onCustom={() => onAction({ action: "select_category", revision: draft.revision, selection: { kind: "custom" } })}
            onSelect={(category) => onAction({ action: "select_category", revision: draft.revision, selection: { kind: "existing", id: category.id, version: category.version } })}
          />
        </div>
      );
    case "custom_category":
      return (
        <div className="space-y-5">
          <StepIntro title="Новая категория" text="Введите короткое и понятное название. После сохранения категория появится в списке." />
          <TextEntry disabled={disabled} label="Название категории" onSubmit={(value) => inputText(value, "categories")} placeholder="Например, Обучение" />
        </div>
      );
    case "wizard_account":
    case "quick_account":
    case "account_required":
    case "review_account":
    case "edit_account":
      return (
        <div className="space-y-5">
          <StepIntro title="Счёт" text="Выберите счёт из списка или создайте новый." />
          <AccountSelector
            allowCustom={draft.state !== "edit_account"}
            disabled={disabled}
            onCustom={() => onAction({ action: "select_account", revision: draft.revision, selection: { kind: "custom" } })}
            onSelect={(account) => onAction({ action: "select_account", revision: draft.revision, selection: { kind: "existing", id: account.id, version: account.version } })}
          />
        </div>
      );
    case "custom_account":
      return (
        <div className="space-y-5">
          <StepIntro title="Новый счёт" text="Введите понятное название. Новый счёт будет создан в вашей основной валюте." />
          <TextEntry disabled={disabled} label="Название счёта" onSubmit={(value) => inputText(value, "accounts")} placeholder="Например, Основной" />
        </div>
      );
    case "wizard_date":
    case "review_date":
    case "edit_date_menu":
      return (
        <div className="space-y-5">
          <StepIntro title="Дата" text="Быстрый выбор или точная календарная дата." />
          <DatePresetSelector disabled={disabled} onSelect={(value) => onAction({ action: "select_date", revision: draft.revision, value })} />
        </div>
      );
    case "custom_date":
    case "review_date_input":
    case "edit_date":
      return (
        <div className="space-y-5">
          <StepIntro title="Точная дата" text="Выберите день операции в календаре." />
          <DateEntry disabled={disabled} onSubmit={inputText} />
        </div>
      );
    case "wizard_description":
      return (
        <div className="space-y-5">
          <StepIntro title="Описание" text="Добавьте короткое пояснение или пропустите этот шаг." />
          <TextEntry disabled={disabled} label="Описание" multiline onSubmit={inputText} placeholder="Необязательная заметка" />
          <button className="button button--ghost w-full" disabled={disabled} onClick={() => navigate("skip_description")} type="button">Без описания</button>
        </div>
      );
    case "edit_description":
      return (
        <div className="space-y-5">
          <StepIntro title="Изменить описание" text="Для пустого описания используйте символ «-»." />
          <TextEntry disabled={disabled} label="Описание" multiline onSubmit={inputText} placeholder="Новое описание или -" submitLabel="Сохранить" />
        </div>
      );
    case "wizard_confirm":
    case "quick_confirm":
    case "review":
      return (
        <div className="space-y-5">
          <StepIntro title="Проверьте операцию" text="Сохранение доступно только после явного подтверждения." />
          <DraftReview draft={draft} />
          <ReviewActions disabled={disabled} onAction={navigate} restricted={draft.flow === "bank_import"} />
          {draft.flow === "bank_import" ? null : <RuleControls draft={draft} disabled={disabled} onAction={(value) => onAction({ action: "set_rule", revision: draft.revision, value })} />}
          <button className="button button--primary w-full" disabled={disabled} onClick={onConfirm} type="button">Подтвердить и сохранить</button>
        </div>
      );
    case "edit_menu":
      return (
        <div className="space-y-5">
          <StepIntro title="Что изменить" text="Выберите поле. Каждое изменение сохраняется отдельно." />
          <DraftReview draft={draft} />
          <ReviewActions disabled={disabled} onAction={navigate} />
        </div>
      );
    case "edit_type":
      return <EditTypeStep disabled={disabled} draft={draft} onAction={(action) => onAction(action)} />;
    case "unsupported":
      return <UnsupportedState onClose={onCloseTelegram} />;
  }
}

function UnsupportedState({ onClose }: { onClose(): void }) {
  return (
    <section className="state-panel" role="alert">
      <span aria-hidden="true" className="state-panel__mark">↗</span>
      <div>
        <h2 className="state-panel__title">Продолжите в Telegram</h2>
        <p className="state-panel__description">Этот сценарий пока доступен только в чате с ботом. Mini App не меняет его данные.</p>
        <button className="button button--secondary mt-4" onClick={onClose} type="button">Вернуться в Telegram</button>
      </div>
    </section>
  );
}
