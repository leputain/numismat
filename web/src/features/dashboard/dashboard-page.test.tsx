// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import type { PropsWithChildren } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type {
  Budget,
  BudgetPageResponse,
  DashboardResponse,
  Transaction,
} from "../../shared/api/types";

const apiGet = vi.hoisted(() => vi.fn());
const executeMutation = vi.hoisted(() => vi.fn());
const prepareMutation = vi.hoisted(() => vi.fn(() => ({ execute: executeMutation })));

vi.mock("../../app/providers", () => ({ apiClient: { get: apiGet, prepareMutation } }));
vi.mock("../../shared/auth/use-session-format", () => ({
  useSessionFormat: () => ({
    baseCurrency: "RUB",
    locale: "ru-RU",
    timeZone: "Europe/Moscow",
  }),
}));

import { DashboardPage } from "./dashboard-page";

const CURSOR = "B".repeat(76);

function transaction(index: number): Transaction {
  const suffix = String(index + 1).padStart(12, "0");
  return {
    account: { id: `018f0000-0000-7000-8000-${suffix}`, name: `Счёт ${String(index + 1)}` },
    amount_minor: String((index + 1) * 10_000),
    category: {
      emoji: "●",
      id: `028f0000-0000-7000-8000-${suffix}`,
      name: `Категория ${String(index + 1)}`,
    },
    currency: "RUB",
    deleted_at: null,
    description: "",
    id: `038f0000-0000-7000-8000-${suffix}`,
    occurred_at: "2026-08-24T09:00:00.000Z",
    source: "manual",
    type: index % 2 === 0 ? "expense" : "income",
    version: index + 1,
  };
}

function budget(index: number, state: "on_track" | "watch" | "over"): Budget {
  return {
    category_id: null,
    created_at: "2026-08-01T00:00:00.000Z",
    currency: "RUB",
    deleted_at: null,
    ends_on: "2026-08-31",
    id: `048f0000-0000-7000-8000-${String(index + 1).padStart(12, "0")}`,
    limit_minor: "100000",
    name: `Бюджет ${String(index + 1)}`,
    progress: {
      cutoff_at: "2026-08-24T12:00:00.000Z",
      forecast_minor: state === "over" ? "110000" : "70000",
      known_recurring_minor: "10000",
      measured_at: "2026-08-24T12:00:00.000Z",
      overspent_minor: state === "over" ? "10000" : "0",
      progress_bps: state === "over" ? 11_000 : 7_000,
      remaining_minor: state === "over" ? "0" : "30000",
      safe_daily_minor: state === "on_track" ? "1000" : "0",
      spent_minor: state === "over" ? "110000" : "70000",
      state,
    },
    starts_on: "2026-08-01",
    timezone: "Europe/Moscow",
    updated_at: "2026-08-24T12:00:00.000Z",
    version: 1,
  };
}

const DASHBOARD: DashboardResponse = {
  comparable_period: {
    end: "2026-08-01T00:00:00.000Z",
    start: "2026-07-01T00:00:00.000Z",
    totals: [{ currency: "RUB", expense_minor: "40000", income_minor: "90000", net_minor: "50000" }],
  },
  current_period: {
    end: "2026-09-01T00:00:00.000Z",
    start: "2026-08-01T00:00:00.000Z",
    totals: [{ currency: "RUB", expense_minor: "50000", income_minor: "100000", net_minor: "50000" }],
  },
  recent_transactions: [transaction(0), transaction(1), transaction(2), transaction(3)],
  top_categories: [],
};

const INCOMPLETE_BUDGETS: BudgetPageResponse = {
  items: [budget(0, "on_track"), budget(1, "on_track"), budget(2, "watch"), budget(3, "over")],
  measured_at: "2026-08-24T12:00:00.000Z",
  next_cursor: CURSOR,
  window: { ends_on: "2026-08-31", starts_on: "2026-08-01" },
};

function renderPage() {
  const client = new QueryClient({
    defaultOptions: {
      mutations: { retry: false },
      queries: { gcTime: 60_000, retry: false, staleTime: 0 },
    },
  });
  const Wrapper = ({ children }: PropsWithChildren) => (
    <QueryClientProvider client={client}>
      <MemoryRouter>{children}</MemoryRouter>
    </QueryClientProvider>
  );
  render(<DashboardPage />, { wrapper: Wrapper });
}

beforeEach(() => {
  apiGet.mockImplementation(async (pathValue: unknown) => {
    const path = String(pathValue);
    if (path === "/api/v1/dashboard") return DASHBOARD;
    if (path === "/api/v1/drafts/active") return { draft: null };
    if (path.startsWith("/api/v1/budgets?")) return INCOMPLETE_BUDGETS;
    throw new Error(`Unexpected synthetic API path: ${path}`);
  });
  executeMutation.mockResolvedValue({
    result: {
      draft_id: "058f0000-0000-7000-8000-000000000001",
      kind: "draft",
      revision: 1,
    },
  });
});

afterEach(() => {
  cleanup();
  apiGet.mockReset();
  executeMutation.mockReset();
  prepareMutation.mockClear();
  vi.restoreAllMocks();
});

describe("dashboard page", () => {
  it("requests a bounded budget page, fails closed on a cursor and keeps quick capture plus three recent rows", async () => {
    renderPage();
    await screen.findByRole("heading", { name: "Три последние операции" });

    const budgetPath = apiGet.mock.calls
      .map((call) => String(call[0]))
      .find((path) => path.startsWith("/api/v1/budgets?"));
    expect(budgetPath).toBeDefined();
    const params = new URL(budgetPath!, "https://numismat.invalid").searchParams;
    expect(params.get("limit")).toBe("50");
    expect(params.get("deleted")).toBe("false");
    expect(params.get("starts_on")).toMatch(/^\d{4}-\d{2}-01$/u);
    expect(params.get("ends_on")).toMatch(/^\d{4}-\d{2}-\d{2}$/u);

    expect(await screen.findByText("Проверьте список бюджетов")).not.toBeNull();
    expect(screen.queryByText("Лимит уже превышен")).toBeNull();
    expect(screen.getAllByRole("link", { name: /^(Расход|Доход):/u })).toHaveLength(3);
    expect(screen.queryByText("Категория 4")).toBeNull();

    fireEvent.change(screen.getByLabelText("Сумма или короткая запись"), {
      target: { value: "  500  " },
    });
    fireEvent.click(screen.getByRole("button", { name: "Продолжить" }));
    await waitFor(() => expect(prepareMutation).toHaveBeenCalledOnce());
    expect(prepareMutation).toHaveBeenCalledWith(
      "/api/v1/drafts/quick",
      { text: "500" },
      "POST",
    );
  });
});
