import type { Draft } from "../../shared/api/types";

const DRAFT_PROGRESS_STEPS = [
  "Тип",
  "Сумма",
  "Категория",
  "Счёт",
  "Дата",
  "Описание",
  "Проверка",
] as const;
const QUICK_PROGRESS_STEPS = ["Категория", "Счёт", "Проверка"] as const;
const AMOUNT_ONLY_PROGRESS_STEPS = ["Тип", "Категория", "Счёт", "Дата", "Описание"] as const;
const AMOUNT_ONLY_REVIEW_STEPS = ["Тип", "Категория", "Счёт", "Дата", "Проверка"] as const;
const REVIEW_EDIT_PROGRESS_STEPS = ["Изменение", "Проверка"] as const;

export interface DraftProgress {
  readonly currentIndex: number;
  readonly steps: readonly string[];
}

const DRAFT_STATE_PROGRESS: Partial<Record<Draft["state"], DraftProgress>> = {
  wizard_type: { currentIndex: 0, steps: DRAFT_PROGRESS_STEPS },
  wizard_amount: { currentIndex: 1, steps: DRAFT_PROGRESS_STEPS },
  wizard_category: { currentIndex: 2, steps: DRAFT_PROGRESS_STEPS },
  custom_category: { currentIndex: 2, steps: DRAFT_PROGRESS_STEPS },
  wizard_account: { currentIndex: 3, steps: DRAFT_PROGRESS_STEPS },
  custom_account: { currentIndex: 3, steps: DRAFT_PROGRESS_STEPS },
  wizard_date: { currentIndex: 4, steps: DRAFT_PROGRESS_STEPS },
  custom_date: { currentIndex: 4, steps: DRAFT_PROGRESS_STEPS },
  wizard_description: { currentIndex: 5, steps: DRAFT_PROGRESS_STEPS },
  wizard_confirm: { currentIndex: 6, steps: DRAFT_PROGRESS_STEPS },
  quick_category: { currentIndex: 0, steps: QUICK_PROGRESS_STEPS },
  category_required: { currentIndex: 0, steps: QUICK_PROGRESS_STEPS },
  quick_account: { currentIndex: 1, steps: QUICK_PROGRESS_STEPS },
  account_required: { currentIndex: 1, steps: QUICK_PROGRESS_STEPS },
  quick_confirm: { currentIndex: 2, steps: QUICK_PROGRESS_STEPS },
  review_type: { currentIndex: 0, steps: REVIEW_EDIT_PROGRESS_STEPS },
  review_amount: { currentIndex: 0, steps: REVIEW_EDIT_PROGRESS_STEPS },
  review_category: { currentIndex: 0, steps: REVIEW_EDIT_PROGRESS_STEPS },
  review_account: { currentIndex: 0, steps: REVIEW_EDIT_PROGRESS_STEPS },
  review_date: { currentIndex: 0, steps: REVIEW_EDIT_PROGRESS_STEPS },
  review_date_input: { currentIndex: 0, steps: REVIEW_EDIT_PROGRESS_STEPS },
};

const AMOUNT_ONLY_STATE_PROGRESS: Partial<Record<Draft["state"], number>> = {
  wizard_type: 0,
  wizard_category: 1,
  custom_category: 1,
  wizard_account: 2,
  custom_account: 2,
  wizard_date: 3,
  custom_date: 3,
  wizard_description: 4,
  wizard_confirm: 4,
};

/**
 * Raw draft payloads are intentionally private. The public projection keeps
 * amount-only drafts distinguishable as quick flow entering wizard states
 * with an already projected amount.
 */
export function draftProgressFor(
  draft: Pick<Draft, "flow" | "state" | "transaction">,
): DraftProgress | undefined {
  const amountOnlyIndex = AMOUNT_ONLY_STATE_PROGRESS[draft.state];
  if (
    draft.flow === "quick" &&
    draft.transaction?.amount_minor != null &&
    amountOnlyIndex !== undefined
  ) {
    return {
      currentIndex: amountOnlyIndex,
      steps:
        draft.state === "wizard_confirm"
          ? AMOUNT_ONLY_REVIEW_STEPS
          : AMOUNT_ONLY_PROGRESS_STEPS,
    };
  }
  return DRAFT_STATE_PROGRESS[draft.state];
}
