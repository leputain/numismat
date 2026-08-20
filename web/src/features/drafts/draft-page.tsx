import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { useNavigate } from "react-router";

import { apiClient } from "../../app/providers";
import type { ActiveDraftResponse, Draft, DraftAction, MutationResponse } from "../../shared/api/types";
import { useAuth } from "../auth/auth-context";
import { EmptyState, ErrorState, PageSkeleton } from "../../shared/components/async-state";
import { MutationFeedback } from "../../shared/components/mutation-feedback";
import { PageHeading } from "../../shared/components/page-heading";
import { isOptimisticConflict } from "../../shared/errors/user-message";
import { emitClientEvent } from "../../shared/logging/client-events";
import { usePreparedMutation } from "../../shared/mutations/prepared-mutation";
import {
  refreshDraftQueries,
  refreshFinanceQueries,
  restartTransactionPagination,
} from "../../shared/mutations/query-recovery";
import { queryKeys } from "../../shared/queries/query-keys";
import { DraftStateRenderer } from "./draft-state-renderer";

type DraftMutationKind = "cancel" | "confirm" | "create" | "patch" | "replace" | "resume";
interface DraftMutationContext {
  readonly kind: DraftMutationKind;
  readonly catalog?: "accounts" | "categories";
}

const STATE_LABELS: Record<Draft["state"], string> = {
  wizard_type: "Тип",
  wizard_amount: "Сумма",
  wizard_category: "Категория",
  custom_category: "Новая категория",
  wizard_account: "Счёт",
  custom_account: "Новый счёт",
  wizard_date: "Дата",
  custom_date: "Дата",
  wizard_description: "Описание",
  wizard_confirm: "Проверка",
  quick_category: "Категория",
  category_required: "Категория",
  quick_account: "Счёт",
  account_required: "Счёт",
  quick_confirm: "Проверка",
  review: "Проверка",
  review_type: "Тип",
  review_amount: "Сумма",
  review_category: "Категория",
  review_account: "Счёт",
  review_date: "Дата",
  review_date_input: "Дата",
  edit_menu: "Редактирование",
  edit_type: "Тип и категория",
  edit_amount: "Сумма",
  edit_category: "Категория",
  edit_account: "Счёт",
  edit_date_menu: "Дата",
  edit_date: "Дата",
  edit_description: "Описание",
  unsupported: "Telegram",
};

const BACK_STATES = new Set<Draft["state"]>([
  "review_type",
  "review_amount",
  "review_category",
  "review_account",
  "review_date",
  "review_date_input",
  "wizard_amount",
  "wizard_category",
  "custom_category",
  "wizard_account",
  "quick_account",
  "account_required",
  "custom_account",
  "custom_date",
  "wizard_date",
  "wizard_description",
  "wizard_confirm",
  "quick_confirm",
  "review",
  "quick_category",
  "category_required",
  "edit_type",
  "edit_amount",
  "edit_category",
  "edit_account",
  "edit_date_menu",
  "edit_date",
  "edit_description",
]);

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
const REVIEW_EDIT_PROGRESS_STEPS = ["Изменение", "Проверка"] as const;

interface DraftProgress {
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

function DraftProgressGuide({ state }: { readonly state: Draft["state"] }) {
  const progress = DRAFT_STATE_PROGRESS[state];
  if (progress === undefined) {
    return null;
  }
  const currentStep = progress.steps[progress.currentIndex];
  if (currentStep === undefined) {
    return null;
  }
  const nextStep = progress.steps[progress.currentIndex + 1];
  return (
    <section
      aria-label={`Ход заполнения: шаг ${String(progress.currentIndex + 1)} из ${String(progress.steps.length)}`}
      className="draft-progress"
    >
      <div className="draft-progress__copy">
        <span className="draft-progress__position">
          Шаг {progress.currentIndex + 1} из {progress.steps.length}
        </span>
        <strong className="draft-progress__current">{currentStep}</strong>
        <span className="draft-progress__next">
          {nextStep === undefined ? "Осталось проверить и сохранить" : `Далее: ${nextStep}`}
        </span>
      </div>
      <ol aria-hidden="true" className="draft-progress__track">
        {progress.steps.map((step, index) => (
          <li
            className={`draft-progress__segment${index < progress.currentIndex ? " is-complete" : ""}${index === progress.currentIndex ? " is-current" : ""}`}
            key={step}
          />
        ))}
      </ol>
    </section>
  );
}

export function DraftPage() {
  const auth = useAuth();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [cancelDialog, setCancelDialog] = useState(false);
  const activeDraft = useQuery({
    queryKey: queryKeys.drafts.active,
    queryFn: ({ signal }) => apiClient.get<ActiveDraftResponse>("/api/v1/drafts/active", { signal }),
    staleTime: 0,
  });
  const mutation = usePreparedMutation<MutationResponse | undefined, DraftMutationContext>({
    eventScope: "draft",
    onSuccess: async (response, context) => {
      setCancelDialog(false);
      await restartTransactionPagination(queryClient);
      if (context.catalog === "accounts") {
        await queryClient.invalidateQueries({ queryKey: queryKeys.catalogs.accounts });
      }
      if (context.catalog === "categories") {
        await queryClient.invalidateQueries({ queryKey: queryKeys.catalogs.all });
      }
      await refreshDraftQueries(queryClient);
      if (response?.result.kind === "transaction") {
        await refreshFinanceQueries(queryClient);
        navigate(`/transactions/${response.result.transaction_id}`);
      }
    },
    onRejected: async (error) => {
      await restartTransactionPagination(queryClient);
      if (isOptimisticConflict(error)) {
        emitClientEvent("mutation_conflict_detected");
      }
      await refreshDraftQueries(queryClient);
    },
    onOutcomeUnknown: async (context) => {
      await restartTransactionPagination(queryClient);
      await Promise.all([refreshDraftQueries(queryClient), refreshFinanceQueries(queryClient)]);
      if (context.catalog !== undefined) {
        await queryClient.invalidateQueries({ queryKey: queryKeys.catalogs.all });
      }
    },
  });

  useEffect(() => emitClientEvent("draft_opened"), []);

  if (activeDraft.isPending) {
    return <PageSkeleton rows={4} />;
  }
  if (activeDraft.isError) {
    return <ErrorState onAction={() => void activeDraft.refetch()} />;
  }

  const draft = activeDraft.data.draft;
  const disabled = mutation.isPending || mutation.outcomeUnknown;
  const runRevisionMutation = (kind: Exclude<DraftMutationKind, "create" | "patch">) => {
    if (draft === null) {
      return;
    }
    mutation.run({
      path: `/api/v1/drafts/${draft.id}/${kind}`,
      body: { revision: draft.revision },
      context: { kind },
    });
  };
  const patchDraft = (action: DraftAction, catalog?: "accounts" | "categories") => {
    if (draft === null) {
      return;
    }
    mutation.run({
      path: `/api/v1/drafts/${draft.id}`,
      body: action,
      method: "PATCH",
      context: catalog === undefined ? { kind: "patch" } : { kind: "patch", catalog },
    });
  };

  if (draft === null) {
    return (
      <div className="page-stack page-stack--narrow draft-page">
        <PageHeading
          description="Каждая операция сначала собирается в черновике. Вы проверяете данные и только потом сохраняете запись."
          eyebrow="Безопасный ввод"
          title="Новая операция"
        />
        <EmptyState title="Активного черновика нет">
          <p>Начните пошаговое добавление. До вашего подтверждения запись не появится в истории.</p>
          <button
            className="button button--primary mt-5"
            disabled={disabled}
            onClick={() => mutation.run({ path: "/api/v1/drafts", body: {}, context: { kind: "create" } })}
            type="button"
          >
            {mutation.isPending ? "Создаём…" : "Начать черновик"}
          </button>
        </EmptyState>
        <MutationFeedback
          error={mutation.error}
          onRetryUnknown={mutation.retryUnknown}
          outcomeUnknown={mutation.outcomeUnknown}
          pending={mutation.isPending}
        />
      </div>
    );
  }

  return (
    <div className="page-stack page-stack--narrow draft-page">
      <PageHeading
        description={draft.flow === "transaction_edit" ? "Изменение уже сохранённой операции" : draft.flow === "bank_import" ? "Проверка строки из банковского файла. Сумма, счёт и дата уже заданы." : "Заполните данные по шагам и проверьте итог перед сохранением."}
        eyebrow="Активный черновик"
        title={STATE_LABELS[draft.state]}
      />

      {draft.conflict == null && !draft.suspended && draft.supported && draft.state !== "unsupported" ? (
        <DraftProgressGuide state={draft.state} />
      ) : null}

      {draft.conflict !== null && draft.conflict !== undefined ? (
        <section aria-labelledby="conflict-title" className="conflict-card" role="alertdialog">
          <p className="eyebrow">Конфликт сценариев</p>
          <h2 className="section-title" id="conflict-title">Есть новое действие и незавершённый черновик</h2>
          <p className="page-description">
            Продолжите текущий черновик или замените его новым. Без вашего выбора ничего не изменится.
          </p>
          <div className="dialog__actions">
            <button className="button button--secondary" disabled={disabled} onClick={() => runRevisionMutation("resume")} type="button">Продолжить текущий</button>
            <button className="button button--danger" disabled={disabled} onClick={() => runRevisionMutation("replace")} type="button">Заменить новым</button>
          </div>
        </section>
      ) : draft.suspended ? (
        <section className="notice notice--warning" role="status">
          <div>
            <p className="notice__title">Черновик приостановлен</p>
            <p className="notice__text">Продолжение откроется с последнего сохранённого шага.</p>
          </div>
          <button className="button button--secondary" disabled={disabled} onClick={() => runRevisionMutation("resume")} type="button">Продолжить</button>
        </section>
      ) : !draft.supported || draft.flow === "unsupported" ? (
        <section className="state-panel" role="alert">
          <span aria-hidden="true" className="state-panel__mark">↗</span>
          <div>
            <h2 className="state-panel__title">Продолжите в Telegram</h2>
            <p className="state-panel__description">Этот сценарий пока можно завершить только в чате с ботом. Здесь его данные не изменялись.</p>
            <button className="button button--secondary mt-4" onClick={auth.closeMiniApp} type="button">Вернуться в Telegram</button>
          </div>
        </section>
      ) : (
        <section className="draft-step">
          <DraftStateRenderer
            disabled={disabled}
            draft={draft}
            key={`${draft.id}:${draft.revision}:${draft.state}`}
            onAction={patchDraft}
            onCloseTelegram={auth.closeMiniApp}
            onConfirm={() => runRevisionMutation("confirm")}
          />
        </section>
      )}

      <MutationFeedback
        error={mutation.error}
        onRetryUnknown={mutation.retryUnknown}
        outcomeUnknown={mutation.outcomeUnknown}
        pending={mutation.isPending}
      />

      {draft.conflict == null && !draft.suspended && draft.supported && draft.flow !== "unsupported" && draft.state !== "unsupported" ? (
        <div className="draft-footer-actions">
          {BACK_STATES.has(draft.state) ? (
            <button className="button button--ghost" disabled={disabled} onClick={() => patchDraft({ action: "back", revision: draft.revision })} type="button">Назад</button>
          ) : null}
          <button className="button button--danger-ghost" disabled={disabled} onClick={() => setCancelDialog(true)} type="button">Отменить черновик</button>
        </div>
      ) : null}

      {cancelDialog ? (
        <div className="dialog-backdrop">
          <section
            aria-labelledby="cancel-title"
            aria-modal="true"
            className="dialog"
            onKeyDown={(event) => {
              if (event.key === "Escape") {
                setCancelDialog(false);
              }
            }}
            role="dialog"
          >
            <p className="eyebrow">Без сохранения</p>
            <h2 className="section-title" id="cancel-title">Отменить черновик?</h2>
            <p className="page-description">Собранные данные будут удалены, финансовая операция не появится.</p>
            <div className="dialog__actions">
              <button autoFocus className="button button--secondary" onClick={() => setCancelDialog(false)} type="button">Продолжить ввод</button>
              <button className="button button--danger" onClick={() => runRevisionMutation("cancel")} type="button">Отменить</button>
            </div>
          </section>
        </div>
      ) : null}
    </div>
  );
}
