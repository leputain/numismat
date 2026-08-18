import { useMutation } from "@tanstack/react-query";
import { useCallback, useState } from "react";

import type { PreparedRawMutation, RawMutationContentType } from "../../api/client";
import { apiClient } from "../../app/providers";
import { isUnknownMutationOutcome } from "../errors/user-message";
import { emitClientEvent } from "../logging/client-events";

interface RawMutationRequest<TContext> {
  readonly path: string;
  readonly content: Uint8Array;
  readonly contentType: RawMutationContentType;
  readonly context: TContext;
}

interface RawMutationIntent<TResponse, TContext> {
  readonly prepared: PreparedRawMutation<TResponse>;
  readonly context: TContext;
}

interface PreparedRawMutationOptions<TResponse, TContext> {
  onSuccess(response: TResponse, context: TContext): void | Promise<void>;
  onRejected(error: unknown, context: TContext): void | Promise<void>;
  onOutcomeUnknown(context: TContext): void | Promise<void>;
}

/**
 * Keeps one copied opaque body and one idempotency key in memory only. The
 * intent is never serialized, queued offline, or decorated with file metadata.
 */
export function usePreparedRawMutation<TResponse, TContext>(
  options: PreparedRawMutationOptions<TResponse, TContext>,
) {
  const [unknownIntent, setUnknownIntent] = useState<
    RawMutationIntent<TResponse, TContext>
  >();
  const mutation = useMutation<
    TResponse,
    unknown,
    RawMutationIntent<TResponse, TContext>
  >({
    mutationFn: (intent) => intent.prepared.execute(),
    onSuccess: async (response, intent) => {
      setUnknownIntent(undefined);
      emitClientEvent("bank_import_action_completed");
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
      emitClientEvent("bank_import_action_rejected");
      await options.onRejected(error, intent.context);
    },
    retry: false,
  });

  const run = useCallback(
    ({ path, content, contentType, context }: RawMutationRequest<TContext>) => {
      setUnknownIntent(undefined);
      emitClientEvent("bank_import_action_started");
      mutation.mutate({
        prepared: apiClient.prepareRawMutation<TResponse>(path, content, contentType),
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
