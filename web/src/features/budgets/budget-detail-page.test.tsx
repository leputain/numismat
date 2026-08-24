// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import type { PropsWithChildren } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { Budget } from "../../shared/api/types";
import { formatMoney } from "../../shared/finance/money";

const apiGet = vi.hoisted(() => vi.fn());

vi.mock("../../app/providers", () => ({
  apiClient: { get: apiGet, prepareMutation: vi.fn() },
}));
vi.mock("../../shared/auth/use-session-format", () => ({
  useSessionFormat: () => ({
    baseCurrency: "RUB",
    locale: "en-US",
    timeZone: "Europe/Moscow",
  }),
}));

import { BudgetDetailPage } from "./budget-detail-page";

const BUDGET_ID = "018f0000-0000-7000-8000-000000000010";
const SPENT = "900719925474099312345";

const BUDGET: Budget = {
  category_id: null,
  created_at: "2026-08-01T00:00:00.000Z",
  currency: "RUB",
  deleted_at: null,
  ends_on: "2026-08-31",
  id: BUDGET_ID,
  limit_minor: "1801439850948198624691",
  name: "Большой точный бюджет",
  progress: {
    cutoff_at: "2026-08-24T12:00:00.000Z",
    forecast_minor: "1801439850948198624691",
    known_recurring_minor: "900719925474099312346",
    measured_at: "2026-08-24T12:00:00.000Z",
    overspent_minor: "0",
    progress_bps: 5_000,
    remaining_minor: "900719925474099312346",
    safe_daily_minor: "0",
    spent_minor: SPENT,
    state: "watch",
  },
  starts_on: "2026-08-01",
  timezone: "Europe/Moscow",
  updated_at: "2026-08-24T12:00:00.000Z",
  version: 2,
};

function renderDetail() {
  const client = new QueryClient({
    defaultOptions: { mutations: { retry: false }, queries: { retry: false } },
  });
  const Wrapper = ({ children }: PropsWithChildren) => (
    <QueryClientProvider client={client}>
      <MemoryRouter>{children}</MemoryRouter>
    </QueryClientProvider>
  );
  render(<BudgetDetailPage budgetId={BUDGET_ID} />, { wrapper: Wrapper });
}

beforeEach(() => {
  apiGet.mockResolvedValue(BUDGET);
});

afterEach(() => {
  cleanup();
  apiGet.mockReset();
  vi.restoreAllMocks();
});

describe("budget detail forecast", () => {
  it("shows authoritative state, all forecast metrics and exact oversized money", async () => {
    renderDetail();

    expect(await screen.findByText("Большой точный бюджет")).not.toBeNull();
    expect(screen.getByLabelText("Статус бюджета: Нужен запас")).not.toBeNull();
    expect(screen.getByText(formatMoney(SPENT, "RUB", "en-US"))).not.toBeNull();
    expect(screen.getByText("Известные регулярные")).not.toBeNull();
    expect(screen.getByText("Прогноз к концу периода")).not.toBeNull();
    expect(screen.getByText("Безопасно в день")).not.toBeNull();
    expect(screen.getByText(/Повседневные расходы вперёд не экстраполируются/u)).not.toBeNull();
    expect(screen.getByText("RUB · отдельно от других валют")).not.toBeNull();
    expect(apiGet).toHaveBeenCalledWith(`/api/v1/budgets/${BUDGET_ID}`, expect.any(Object));
  });
});
