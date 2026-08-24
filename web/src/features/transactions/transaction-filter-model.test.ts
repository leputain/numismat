import { describe, expect, it } from "vitest";

import { queryKeys } from "../../shared/queries/query-keys";
import {
  activeTransactionFilterCount,
  budgetTransactionsPath,
  buildTransactionQueryFilters,
  createTransactionFilterState,
  transactionPagePath,
  updateCustomTransactionPeriod,
} from "./transaction-filter-model";
import type { TransactionFilterState, TransactionQueryFilters } from "./transaction-filter-model";

const ANCHOR = new Date("2026-08-24T12:00:00.000Z");
const ACCOUNT_ID = "018f0000-0000-7000-8000-000000000010";
const CATEGORY_ID = "018f0000-0000-7000-8000-000000000020";

describe("transaction filter model", () => {
  it("builds stable owner-local presets and a canonical query-key object", () => {
    const initial = createTransactionFilterState("Europe/Moscow", ANCHOR);
    const state: TransactionFilterState = {
      ...initial,
      period: "week",
      type: "expense",
      accountId: ACCOUNT_ID,
      categoryId: CATEGORY_ID,
      currency: "RUB",
    };
    const filters = buildTransactionQueryFilters(state, "Europe/Moscow", ANCHOR);

    expect(initial).toMatchObject({ customStart: "2026-07-26", customEnd: "2026-08-24" });
    expect(filters).toEqual({
      start: "2026-08-17T21:00:00.000Z",
      end: "2026-08-24T21:00:00.000Z",
      type: "expense",
      accountId: ACCOUNT_ID,
      categoryId: CATEGORY_ID,
      currency: "RUB",
    });
    expect(Object.keys(filters)).toEqual([
      "start",
      "end",
      "type",
      "accountId",
      "categoryId",
      "currency",
    ]);
    expect(Object.isFrozen(filters)).toBe(true);
    expect(activeTransactionFilterCount(filters)).toBe(5);
    const key = queryKeys.transactions.active(30, filters);
    expect(key).toEqual(["transactions", "active", { filters, limit: 30 }]);
    expect(key[2].filters).toBe(filters);
  });

  it("uses an exclusive local-day boundary across daylight-saving time", () => {
    const initial = createTransactionFilterState(
      "Europe/Berlin",
      new Date("2026-03-29T10:00:00.000Z"),
    );
    const state: TransactionFilterState = {
      ...initial,
      period: "custom",
      customStart: "2026-03-29",
      customEnd: "2026-03-29",
    };

    expect(
      buildTransactionQueryFilters(
        state,
        "Europe/Berlin",
        new Date("2026-03-29T10:00:00.000Z"),
      ),
    ).toMatchObject({
      start: "2026-03-28T23:00:00.000Z",
      end: "2026-03-29T22:00:00.000Z",
    });
  });

  it("keeps custom ranges ordered and below the backend maximum", () => {
    const initial = createTransactionFilterState("UTC", ANCHOR);
    const changed = updateCustomTransactionPeriod(initial, "start", "2025-01-01");
    const reversed = updateCustomTransactionPeriod(changed, "end", "2024-12-01");

    expect(changed).toMatchObject({
      period: "custom",
      customStart: "2025-01-01",
      customEnd: "2026-01-01",
    });
    expect(reversed).toMatchObject({
      customStart: "2024-12-01",
      customEnd: "2024-12-01",
    });
    expect(updateCustomTransactionPeriod(reversed, "start", "not-a-date")).toBe(reversed);
  });

  it("hydrates an exact budget deep link and rejects ambiguous query input", () => {
    const path = budgetTransactionsPath({
      starts_on: "2026-08-01",
      ends_on: "2026-08-31",
      currency: "RUB",
      category_id: CATEGORY_ID,
    });
    const search = new URL(path, "https://numismat.invalid").search;

    expect(createTransactionFilterState("Europe/Moscow", ANCHOR, search)).toMatchObject({
      period: "custom",
      customStart: "2026-08-01",
      customEnd: "2026-08-31",
      type: "expense",
      accountId: null,
      categoryId: CATEGORY_ID,
      currency: "RUB",
    });
    expect(
      createTransactionFilterState(
        "Europe/Moscow",
        ANCHOR,
        `${search}&currency=USD`,
      ),
    ).toMatchObject({ period: "all", type: null, categoryId: null, currency: null });
    expect(
      createTransactionFilterState(
        "Europe/Moscow",
        ANCHOR,
        "?period=custom&start=2026-08-01&end=2026-08-31&type=expense&currency=RUB&next=https://evil.invalid",
      ),
    ).toMatchObject({ period: "all", type: null, categoryId: null, currency: null });
  });

  it("serializes only canonical backend parameters and keeps trash unfiltered", () => {
    const filters: TransactionQueryFilters = {
      start: "2026-08-01T00:00:00.000Z",
      end: "2026-09-01T00:00:00.000Z",
      type: "income",
      accountId: ACCOUNT_ID,
      categoryId: CATEGORY_ID,
      currency: "USD",
    };
    const cursor = "A".repeat(76);
    const active = new URL(
      transactionPagePath("active", cursor, filters, 30),
      "https://numismat.invalid",
    );
    const trash = new URL(
      transactionPagePath("trash", cursor, filters, 30),
      "https://numismat.invalid",
    );

    expect(Object.fromEntries(active.searchParams)).toEqual({
      limit: "30",
      cursor,
      start: filters.start,
      end: filters.end,
      type: "income",
      account_id: ACCOUNT_ID,
      category_id: CATEGORY_ID,
      currency: "USD",
    });
    expect(Object.fromEntries(trash.searchParams)).toEqual({ limit: "30", cursor });
    expect(() =>
      transactionPagePath(
        "active",
        null,
        { ...filters, end: null },
        30,
      ),
    ).toThrowError("Transaction filter period is incomplete");
  });
});
