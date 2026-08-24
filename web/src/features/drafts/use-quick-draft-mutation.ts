import { useQueryClient } from "@tanstack/react-query";

import type { MutationResponse } from "../../shared/api/types";
import { isOptimisticConflict } from "../../shared/errors/user-message";
import { emitClientEvent } from "../../shared/logging/client-events";
import { usePreparedMutation } from "../../shared/mutations/prepared-mutation";
import { refreshDraftQueries } from "../../shared/mutations/query-recovery";

export interface QuickDraftMutationOptions {
  onDraftReady?(response: MutationResponse): void | Promise<void>;
}

/**
 * Canonical Mini App ingress for quick text/amount capture. It deliberately
 * delegates session binding, CSRF, idempotency and ambiguous retries to the
 * shared prepared-mutation boundary.
 */
export function useQuickDraftMutation(options: QuickDraftMutationOptions = {}) {
  const queryClient = useQueryClient();
  const mutation = usePreparedMutation<MutationResponse, "quick">({
    eventScope: "draft",
    onSuccess: async (response) => {
      await refreshDraftQueries(queryClient);
      await options.onDraftReady?.(response);
    },
    onRejected: async (error) => {
      if (isOptimisticConflict(error)) {
        emitClientEvent("mutation_conflict_detected");
      }
      await refreshDraftQueries(queryClient);
    },
    onOutcomeUnknown: async () => {
      await refreshDraftQueries(queryClient);
    },
  });

  return {
    disabled: mutation.isPending || mutation.outcomeUnknown,
    error: mutation.error,
    isPending: mutation.isPending,
    outcomeUnknown: mutation.outcomeUnknown,
    retryUnknown: mutation.retryUnknown,
    submit(text: string): void {
      mutation.run({
        path: "/api/v1/drafts/quick",
        body: { text },
        context: "quick",
      });
    },
  } as const;
}
