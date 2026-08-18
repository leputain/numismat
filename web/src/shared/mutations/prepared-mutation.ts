import { useMutation } from "@tanstack/react-query";
import { useCallback, useState } from "react";

import type { MutationMethod, PreparedMutation } from "../../api/client";
import { apiClient } from "../../app/providers";
import { isUnknownMutationOutcome } from "../errors/user-message";
import { emitClientEvent } from "../logging/client-events";

interface MutationRequest<TContext> {
  readonly path: string;
  readonly body: unknown;
  readonly method?: MutationMethod;
  readonly context: TContext;
}

interface MutationIntent<TResponse, TContext> {
  readonly prepared: PreparedMutation<TResponse>;
  readonly context: TContext;
}

interface PreparedMutationOptions<TResponse, TContext> {
  readonly eventScope: "bank_import" | "budget" | "draft" | "exchange_rate" | "recurring" | "transaction";
  onSuccess(response: TResponse, context: TContext): void | Promise<void>;
  onRejected(error: unknown, context: TContext): void | Promise<void>;
  onOutcomeUnknown(context: TContext): void | Promise<void>;
}

export function usePreparedMutation<TResponse, TContext>(
  options: PreparedMutationOptions<TResponse, TContext>,
) {
  const [unknownIntent, setUnknownIntent] = useState<MutationIntent<TResponse, TContext>>();
  const mutation = useMutation<TResponse, unknown, MutationIntent<TResponse, TContext>>({
    mutationFn: (intent) => intent.prepared.execute(),
    onSuccess: async (response, intent) => {
      setUnknownIntent(undefined);
      emitClientEvent(actionEvent(options.eventScope, "completed"));
      await options.onSuccess(response, intent.context);
    },
    onError: async (error, intent) => {
      if (isUnknownMutationOutcome(error)) {
        setUnknownIntent(intent);
        emitClientEvent("mutation_outcome_unknown");
        await options.onOutcomeUnknown(intent.context);
        return;
      }
      setUnknownIntent(undefined);
      emitClientEvent(actionEvent(options.eventScope, "rejected"));
      await options.onRejected(error, intent.context);
    },
    retry: false,
  });

  const run = useCallback(
    ({ path, body, method = "POST", context }: MutationRequest<TContext>) => {
      setUnknownIntent(undefined);
      emitClientEvent(actionEvent(options.eventScope, "started"));
      mutation.mutate({
        prepared: apiClient.prepareMutation<TResponse>(path, body, method),
        context,
      });
    },
    [mutation],
  );

  const retryUnknown = useCallback(() => {
    if (unknownIntent !== undefined) {
      mutation.mutate(unknownIntent);
    }
  }, [mutation, unknownIntent]);

  return {
    error: unknownIntent === undefined ? mutation.error : null,
    isPending: mutation.isPending,
    outcomeUnknown: unknownIntent !== undefined,
    reset: mutation.reset,
    retryUnknown,
    run,
  } as const;
}

function actionEvent(
  scope: "bank_import" | "budget" | "draft" | "exchange_rate" | "recurring" | "transaction",
  outcome: "completed" | "rejected" | "started",
) {
  return `${scope}_action_${outcome}` as const;
}
