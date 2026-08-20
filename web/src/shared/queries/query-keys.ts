import type { TimeSeriesGrain } from "../api/types";

export const queryKeys = {
  dashboard: ["dashboard"] as const,
  today: ["reports", "today"] as const,
  reportsRoot: ["reports"] as const,
  timeseries: (start: string, end: string, grain: TimeSeriesGrain) =>
    ["reports", "timeseries", { start, end, grain }] as const,
  transactions: {
    all: ["transactions"] as const,
    activeRoot: ["transactions", "active"] as const,
    active: (limit: number) => ["transactions", "active", { limit }] as const,
    trashRoot: ["transactions", "trash"] as const,
    trash: (limit: number) => ["transactions", "trash", { limit }] as const,
    detailRoot: ["transactions", "detail"] as const,
    detail: (id: string) => ["transactions", "detail", id] as const,
  },
  drafts: {
    all: ["drafts"] as const,
    active: ["drafts", "active"] as const,
    detail: (id: string) => ["drafts", "detail", id] as const,
  },
  catalogs: {
    all: ["catalogs"] as const,
    accounts: ["catalogs", "accounts", { archived: false }] as const,
    categories: (kind: "expense" | "income") =>
      ["catalogs", "categories", { kind, archived: false }] as const,
  },
  budgets: {
    all: ["budgets"] as const,
    activeRoot: ["budgets", "active"] as const,
    active: (startsOn: string, endsOn: string, limit: number) =>
      ["budgets", "active", { startsOn, endsOn, limit }] as const,
    trashRoot: ["budgets", "trash"] as const,
    trash: (startsOn: string, endsOn: string, limit: number) =>
      ["budgets", "trash", { startsOn, endsOn, limit }] as const,
    detailRoot: ["budgets", "detail"] as const,
    detail: (id: string) => ["budgets", "detail", id] as const,
  },
  recurring: {
    all: ["recurring"] as const,
    activeRoot: ["recurring", "active"] as const,
    active: (limit: number) => ["recurring", "active", { limit }] as const,
    trashRoot: ["recurring", "trash"] as const,
    trash: (limit: number) => ["recurring", "trash", { limit }] as const,
    detailRoot: ["recurring", "detail"] as const,
    detail: (id: string) => ["recurring", "detail", id] as const,
    instances: (id: string, limit: number) =>
      ["recurring", "instances", id, { limit }] as const,
  },
  exchangeRates: {
    all: ["exchange-rates"] as const,
    sources: ["exchange-rates", "sources"] as const,
    versionsRoot: ["exchange-rates", "versions"] as const,
    versions: (sourceId: string, limit: number) =>
      ["exchange-rates", "versions", sourceId, { limit }] as const,
    detailRoot: ["exchange-rates", "detail"] as const,
    detail: (versionId: string) => ["exchange-rates", "detail", versionId] as const,
    converted: (versionId: string, start: string, end: string) =>
      ["exchange-rates", "converted", versionId, { start, end }] as const,
  },
  bankImports: {
    all: ["bank-imports"] as const,
    batches: (limit: number) => ["bank-imports", "batches", { limit }] as const,
    detailRoot: ["bank-imports", "detail"] as const,
    detail: (batchId: string) => ["bank-imports", "detail", batchId] as const,
    rows: (batchId: string, limit: number) =>
      ["bank-imports", "rows", batchId, { limit }] as const,
    row: (batchId: string, rowId: string) =>
      ["bank-imports", "row", batchId, rowId] as const,
    candidates: (batchId: string, rowId: string) =>
      ["bank-imports", "candidates", batchId, rowId] as const,
  },
} as const;
