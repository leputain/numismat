import { Link } from "react-router";

import type { RecurringSchedule } from "../../shared/api/types";
import { useSessionFormat } from "../../shared/auth/use-session-format";
import { formatMoney } from "../../shared/finance/money";
import {
  formatNominalLocal,
  recurringCadenceLabel,
  recurringStateLabel,
} from "./recurring-format";

export function RecurringCard({ schedule }: { readonly schedule: RecurringSchedule }) {
  const { locale } = useSessionFormat();
  return (
    <Link className="transaction-card block" to={`/recurring/${schedule.id}`}>
      <div className="flex items-start justify-between gap-4">
        <div className="min-w-0">
          <p className="eyebrow">{recurringStateLabel(schedule.state)}</p>
          <h2 className="mt-1 truncate text-lg font-semibold text-stone-100">{schedule.name}</h2>
          <p className="mt-2 text-sm text-stone-500">
            {recurringCadenceLabel(schedule)} · {schedule.local_time} · {schedule.timezone}
          </p>
        </div>
        <p className="shrink-0 font-semibold text-stone-100">
          {formatMoney(schedule.amount_minor, schedule.currency, locale)}
        </p>
      </div>
      <p className="mt-4 text-xs text-stone-500">
        Следующий черновик: {formatNominalLocal(schedule.next_due_local, locale)}
      </p>
    </Link>
  );
}
