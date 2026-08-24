import { describe, expect, it } from "vitest";

import {
  draftPathWithReturn,
  draftReturnDestination,
  parseDraftReturn,
} from "./draft-return";

const ID = "abcdef00-0000-7000-8000-000000000123";

describe("draft return context", () => {
  it("round-trips the two closed return kinds", () => {
    const bankPath = draftPathWithReturn({ kind: "bank_import", batchId: ID });
    const recurringPath = draftPathWithReturn({ kind: "recurring", scheduleId: ID });

    expect(parseDraftReturn(bankPath.slice(bankPath.indexOf("?")))).toEqual({
      kind: "bank_import",
      batchId: ID,
    });
    expect(parseDraftReturn(recurringPath.slice(recurringPath.indexOf("?")))).toEqual({
      kind: "recurring",
      scheduleId: ID,
    });
    expect(draftReturnDestination({ kind: "bank_import", batchId: ID })).toBe(`/imports/${ID}`);
    expect(draftReturnDestination({ kind: "recurring", scheduleId: ID })).toBe(
      `/recurring/${ID}`,
    );
  });

  it.each([
    "",
    "?returnKind=dashboard",
    `?returnKind=bank_import&batchId=${ID}&next=/more`,
    `?returnKind=bank_import&batchId=${ID}&batchId=${ID}`,
    "?returnKind=bank_import&batchId=NOT-A-UUID",
    `?returnKind=recurring&scheduleId=${ID.toUpperCase()}`,
  ])("rejects open, duplicate, malformed or non-canonical state: %s", (search) => {
    expect(parseDraftReturn(search)).toBeNull();
  });
});
