import { Link } from "react-router";

import type { Transaction } from "../../shared/api/types";
import { MutationFeedback } from "../../shared/components/mutation-feedback";
import { formatTransactionDate } from "../../shared/format/date-time";
import { formatTransactionMoney } from "../../shared/finance/money";
import { QuickCaptureForm } from "./quick-capture-form";

const RECENT_TRANSACTION_LIMIT = 3;

interface MutationState {
  readonly error: unknown;
  readonly outcomeUnknown: boolean;
  readonly pending: boolean;
  onRetryUnknown(): void;
}

interface DraftCaptureLandingProps {
  readonly actionsDisabled: boolean;
  readonly locale: string;
  readonly quickMutation: MutationState;
  readonly recentError: boolean;
  readonly recentPending: boolean;
  readonly recentTransactions: readonly Transaction[] | undefined;
  readonly repeatMutation: MutationState;
  readonly timeZone: string;
  onQuickSubmit(text: string): void;
  onRepeat(transaction: Transaction): void;
  onRetryRecent(): void;
  onStartWizard(): void;
}

function RecentTransaction({
  disabled,
  locale,
  onRepeat,
  timeZone,
  transaction,
}: {
  readonly disabled: boolean;
  readonly locale: string;
  readonly timeZone: string;
  readonly transaction: Transaction;
  onRepeat(transaction: Transaction): void;
}) {
  const typeLabel = transaction.type === "income" ? "Доход" : "Расход";
  return (
    <li className={`quick-repeat-card quick-repeat-card--${transaction.type}`}>
      <Link
        aria-label={`Открыть операцию: ${transaction.category.name}`}
        className="quick-repeat-card__main"
        to={`/transactions/${transaction.id}`}
      >
        <span aria-hidden="true" className="quick-repeat-card__emoji">
          {transaction.category.emoji || (transaction.type === "income" ? "↗" : "↘")}
        </span>
        <span className="quick-repeat-card__copy">
          <strong>{transaction.category.name}</strong>
          <span>
            {typeLabel} · {transaction.account.name} ·{" "}
            {formatTransactionDate(transaction.occurred_at, locale, timeZone)}
          </span>
        </span>
        <span className={`quick-repeat-card__amount quick-repeat-card__amount--${transaction.type}`}>
          {formatTransactionMoney(
            transaction.amount_minor,
            transaction.currency,
            transaction.type,
            locale,
          )}
        </span>
      </Link>
      <button
        aria-label={`Повторить операцию: ${transaction.category.name}`}
        className="button button--secondary quick-repeat-card__action"
        disabled={disabled}
        onClick={() => onRepeat(transaction)}
        type="button"
      >
        Повторить
      </button>
    </li>
  );
}

export function DraftCaptureLanding({
  actionsDisabled,
  locale,
  onQuickSubmit,
  onRepeat,
  onRetryRecent,
  onStartWizard,
  quickMutation,
  recentError,
  recentPending,
  recentTransactions,
  repeatMutation,
  timeZone,
}: DraftCaptureLandingProps) {
  const recent = recentTransactions?.slice(0, RECENT_TRANSACTION_LIMIT);
  return (
    <>
      <div className="draft-capture-grid">
        <section aria-labelledby="quick-capture-title" className="quick-capture-hero">
          <p className="eyebrow">Быстрый ввод</p>
          <h2 className="section-title quick-capture-title" id="quick-capture-title">
            Запишите операцию сразу
          </h2>
          <p className="page-description">
            Введите сумму или короткую фразу. Бот соберёт черновик и обязательно покажет
            итог перед сохранением.
          </p>
          <QuickCaptureForm
            busy={quickMutation.pending || quickMutation.outcomeUnknown}
            disabled={actionsDisabled}
            onSubmit={onQuickSubmit}
          />
          <MutationFeedback
            error={quickMutation.error}
            onRetryUnknown={quickMutation.onRetryUnknown}
            outcomeUnknown={quickMutation.outcomeUnknown}
            pending={quickMutation.pending}
          />
        </section>

        <section aria-labelledby="quick-repeat-title" className="surface-panel quick-repeat-panel">
          <div className="section-heading section-heading--inside quick-repeat-heading">
            <div>
              <p className="eyebrow">Без повторного ввода</p>
              <h2 className="section-title" id="quick-repeat-title">Последние операции</h2>
            </div>
            <Link className="text-link" to="/transactions">Все</Link>
          </div>
          <p className="quick-repeat-note">Повтор откроется как черновик — сначала проверьте его.</p>
          {recentPending ? (
            <div aria-busy="true" aria-label="Загрузка последних операций" className="quick-repeat-skeleton" role="status">
              <div className="skeleton h-16 w-full" />
              <div className="skeleton h-16 w-full" />
              <div className="skeleton h-16 w-full" />
            </div>
          ) : recentError ? (
            <div className="inline-state" role="alert">
              <div>
                <strong>Последние операции не загрузились</strong>
                <span>Быстрый ввод всё равно доступен.</span>
              </div>
              <button className="button button--secondary" onClick={onRetryRecent} type="button">
                Обновить
              </button>
            </div>
          ) : recent === undefined || recent.length === 0 ? (
            <p className="quick-repeat-empty">После первой сохранённой операции здесь появится быстрый повтор.</p>
          ) : (
            <ul className="quick-repeat-list">
              {recent.map((transaction) => (
                <RecentTransaction
                  disabled={actionsDisabled}
                  key={transaction.id}
                  locale={locale}
                  onRepeat={onRepeat}
                  timeZone={timeZone}
                  transaction={transaction}
                />
              ))}
            </ul>
          )}
          <MutationFeedback
            error={repeatMutation.error}
            onRetryUnknown={repeatMutation.onRetryUnknown}
            outcomeUnknown={repeatMutation.outcomeUnknown}
            pending={repeatMutation.pending}
          />
        </section>
      </div>

      <section className="full-wizard-entry">
        <div>
          <p className="full-wizard-entry__title">Нужен полный контроль?</p>
          <p className="full-wizard-entry__text">
            Пошаговый режим отдельно спросит тип, сумму, категорию, счёт, дату и описание.
          </p>
        </div>
        <button
          className="button button--ghost"
          disabled={actionsDisabled}
          onClick={onStartWizard}
          type="button"
        >
          Открыть полный ввод
        </button>
      </section>
    </>
  );
}
