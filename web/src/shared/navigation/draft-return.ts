const CANONICAL_LOWERCASE_UUID =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

export type DraftReturnContext =
  | { readonly kind: "bank_import"; readonly batchId: string }
  | { readonly kind: "recurring"; readonly scheduleId: string };

function hasExactKeys(params: URLSearchParams, expected: readonly string[]): boolean {
  const keys = Array.from(params.keys()).sort();
  return (
    keys.length === expected.length &&
    keys.every((key, index) => key === expected[index]) &&
    expected.every((key) => params.getAll(key).length === 1)
  );
}

export function draftPathWithReturn(context: DraftReturnContext): string {
  const params = new URLSearchParams({ returnKind: context.kind });
  if (context.kind === "bank_import") {
    params.set("batchId", context.batchId);
  } else {
    params.set("scheduleId", context.scheduleId);
  }
  return `/draft?${params.toString()}`;
}

export function parseDraftReturn(search: string): DraftReturnContext | null {
  const params = new URLSearchParams(search);
  const returnKind = params.get("returnKind");
  if (returnKind === "bank_import") {
    if (!hasExactKeys(params, ["batchId", "returnKind"])) {
      return null;
    }
    const batchId = params.get("batchId");
    return batchId !== null && CANONICAL_LOWERCASE_UUID.test(batchId)
      ? { kind: "bank_import", batchId }
      : null;
  }
  if (returnKind === "recurring") {
    if (!hasExactKeys(params, ["returnKind", "scheduleId"])) {
      return null;
    }
    const scheduleId = params.get("scheduleId");
    return scheduleId !== null && CANONICAL_LOWERCASE_UUID.test(scheduleId)
      ? { kind: "recurring", scheduleId }
      : null;
  }
  return null;
}

export function draftReturnDestination(context: DraftReturnContext): string {
  return context.kind === "bank_import"
    ? `/imports/${context.batchId}`
    : `/recurring/${context.scheduleId}`;
}
