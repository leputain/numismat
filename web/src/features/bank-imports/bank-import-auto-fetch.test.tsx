// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import type { PropsWithChildren } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type {
  BankImportBatch,
  BankImportRow,
  BankImportRowPageResponse,
} from "../../shared/api/types";

const apiGet = vi.hoisted(() => vi.fn());
const prepareMutation = vi.hoisted(() => vi.fn());

vi.mock("../../app/providers", () => ({ apiClient: { get: apiGet, prepareMutation } }));
vi.mock("../../shared/auth/use-session-format", () => ({
  useSessionFormat: () => ({
    baseCurrency: "RUB",
    locale: "ru-RU",
    timeZone: "Europe/Moscow",
  }),
}));

import { BankImportDetailPage } from "./bank-import-detail-page";

const BATCH_ID = "018f0000-0000-7000-8000-000000000040";
const CURSOR = "C".repeat(76);

const BATCH: BankImportBatch = {
  account_id: "018f0000-0000-7000-8000-000000000041",
  cancelled_at: null,
  completed_at: null,
  counts: {
    cancelled: 0,
    confirmed: 1,
    linked: 0,
    pending: 1,
    skipped: 0,
    staged: 0,
    total: 2,
  },
  created_at: "2026-08-24T10:00:00.000Z",
  encoding: "utf-8",
  id: BATCH_ID,
  profile: "canonical_v1",
  state: "open",
  updated_at: "2026-08-24T10:00:00.000Z",
  version: 3,
};

function row(position: number, outcome: "confirmed" | "pending"): BankImportRow {
  const resolved = outcome === "confirmed";
  return {
    amount_minor: position === 1 ? "50000" : "25000",
    batch_id: BATCH_ID,
    created_at: "2026-08-24T10:00:00.000Z",
    currency: "RUB",
    description: `Строка банка ${String(position)}`,
    draft_id: null,
    has_reference: false,
    id: `018f0000-0000-7000-8000-${String(41 + position).padStart(12, "0")}`,
    occurred_at: "2026-08-23T09:00:00.000Z",
    outcome,
    position,
    possible_duplicate: false,
    resolved_at: resolved ? "2026-08-24T11:00:00.000Z" : null,
    state: resolved ? "confirmed" : "pending",
    transaction_id: resolved ? "018f0000-0000-7000-8000-000000000050" : null,
    type: "expense",
    updated_at: "2026-08-24T11:00:00.000Z",
    version: position,
  };
}

const FIRST_PAGE: BankImportRowPageResponse = {
  items: [row(1, "confirmed")],
  next_cursor: CURSOR,
};
const SECOND_PAGE: BankImportRowPageResponse = {
  items: [row(2, "pending")],
  next_cursor: null,
};

function renderPage() {
  const client = new QueryClient({
    defaultOptions: { queries: { gcTime: 60_000, retry: false, staleTime: 0 } },
  });
  const Wrapper = ({ children }: PropsWithChildren) => (
    <QueryClientProvider client={client}>
      <MemoryRouter>{children}</MemoryRouter>
    </QueryClientProvider>
  );
  render(<BankImportDetailPage batchId={BATCH_ID} />, { wrapper: Wrapper });
}

beforeEach(() => {
  apiGet.mockImplementation(async (pathValue: unknown) => {
    const path = String(pathValue);
    if (path === `/api/v1/bank-imports/${BATCH_ID}`) return BATCH;
    if (path === `/api/v1/bank-imports/${BATCH_ID}/rows?limit=20`) return FIRST_PAGE;
    if (path === `/api/v1/bank-imports/${BATCH_ID}/rows?limit=20&cursor=${CURSOR}`) {
      return SECOND_PAGE;
    }
    throw new Error(`Unexpected synthetic API path: ${path}`);
  });
  Object.defineProperty(HTMLElement.prototype, "scrollIntoView", {
    configurable: true,
    value: vi.fn(),
  });
  vi.spyOn(window, "requestAnimationFrame").mockImplementation((callback) => {
    callback(0);
    return 1;
  });
  vi.spyOn(window, "cancelAnimationFrame").mockImplementation(() => undefined);
});

afterEach(() => {
  cleanup();
  apiGet.mockReset();
  prepareMutation.mockReset();
  vi.restoreAllMocks();
});

describe("bank import page continuation", () => {
  it("fetches the next page once when the loaded page has no unresolved row", async () => {
    renderPage();

    expect(await screen.findByText("Строка банка 2")).not.toBeNull();
    await waitFor(() => {
      const rowRequests = apiGet.mock.calls
        .map((call) => String(call[0]))
        .filter((path) => path.includes("/rows?"));
      expect(rowRequests).toEqual([
        `/api/v1/bank-imports/${BATCH_ID}/rows?limit=20`,
        `/api/v1/bank-imports/${BATCH_ID}/rows?limit=20&cursor=${CURSOR}`,
      ]);
    });
    expect(screen.getByRole("progressbar").getAttribute("aria-valuenow")).toBe("50");
    expect(screen.getByRole("button", { name: "Создать черновик" })).not.toBeNull();
  });
});
