import { describe, expect, it } from "vitest";

import { shouldAutoFetchNextBankImportPage } from "./bank-import-detail-page";

describe("bank import automatic continuation", () => {
  it("loads another page only when the loaded rows contain no unresolved action", () => {
    expect(
      shouldAutoFetchNextBankImportPage([{ outcome: "confirmed" }], {
        batchOpen: true,
        hasNextPage: true,
        fetchingNextPage: false,
      }),
    ).toBe(true);
    expect(
      shouldAutoFetchNextBankImportPage([{ outcome: "pending" }], {
        batchOpen: true,
        hasNextPage: true,
        fetchingNextPage: false,
      }),
    ).toBe(false);
    expect(
      shouldAutoFetchNextBankImportPage([{ outcome: "awaiting_review" }], {
        batchOpen: true,
        hasNextPage: true,
        fetchingNextPage: false,
      }),
    ).toBe(false);
  });

  it("does not loop while a page request is running or after the batch closes", () => {
    expect(
      shouldAutoFetchNextBankImportPage([], {
        batchOpen: true,
        hasNextPage: true,
        fetchingNextPage: true,
      }),
    ).toBe(false);
    expect(
      shouldAutoFetchNextBankImportPage([], {
        batchOpen: false,
        hasNextPage: true,
        fetchingNextPage: false,
      }),
    ).toBe(false);
  });
});
