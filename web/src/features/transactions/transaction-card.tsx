import { Link } from "react-router";

import type { Transaction } from "../../shared/api/types";
import { useSessionFormat } from "../../shared/auth/use-session-format";
import { formatTransactionDate } from "../../shared/format/date-time";
import { formatTransactionMoney } from "../../shared/finance/money";

export function TransactionCard({ transaction }: { readonly transaction: Transaction }) {
  const { locale, timeZone } = useSessionFormat();
  const deleted = transaction.deleted_at !== null;
  return (
    <Link
      aria-label={`${transaction.category.name}, ${formatTransactionMoney(transaction.amount_minor, transaction.currency, transaction.type, locale)}`}
      className="transaction-card"
      to={`/transactions/${transaction.id}`}
    >
      <span aria-hidden="true" className="transaction-card__emoji">
        {transaction.category.emoji || (transaction.type === "income" ? "↗" : "↘")}
      </span>
      <span className="min-w-0 flex-1">
        <span className="transaction-card__title-row">
          <span className="transaction-card__title">{transaction.category.name}</span>
          <span
            className={`transaction-card__amount transaction-card__amount--${transaction.type}`}
          >
            {formatTransactionMoney(
              transaction.amount_minor,
              transaction.currency,
              transaction.type,
              locale,
            )}
          </span>
        </span>
        <span className="transaction-card__meta">
          <span>{transaction.account.name}</span>
          <span aria-hidden="true">·</span>
          <time dateTime={transaction.occurred_at}>
            {formatTransactionDate(transaction.occurred_at, locale, timeZone)}
          </time>
          {deleted ? <span className="status-chip status-chip--danger">В корзине</span> : null}
        </span>
        {transaction.description.length === 0 ? null : (
          <span className="transaction-card__description">{transaction.description}</span>
        )}
      </span>
      <span aria-hidden="true" className="transaction-card__chevron">›</span>
    </Link>
  );
}
