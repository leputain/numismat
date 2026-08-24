// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import type { PropsWithChildren } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type {
  AccountsResponse,
  CategoriesResponse,
  Transaction,
  TransactionPageResponse,
} from "../../shared/api/types";

const apiGet = vi.hoisted(() => vi.fn());

vi.mock("../../app/providers", () => ({ apiClient: { get: apiGet } }));
vi.mock("../../shared/auth/use-session-format", () => ({
  useSessionFormat: () => ({
    baseCurrency: "RUB",
    locale: "ru-RU",
    timeZone: "Europe/Moscow",
  }),
}));

import { TransactionsPage } from "./transactions-page";

const CURSOR = "A".repeat(76);
const ACTIVE_ACCOUNT_ID = "018f0000-0000-7000-8000-000000000010";
const ARCHIVED_ACCOUNT_ID = "018f0000-0000-7000-8000-000000000011";
const EXPENSE_CATEGORY_ID = "018f0000-0000-7000-8000-000000000020";
const INCOME_CATEGORY_ID = "018f0000-0000-7000-8000-000000000021";

const TRANSACTION: Transaction = {
  account: { id: ACTIVE_ACCOUNT_ID, name: "Основной" },
  amount_minor: "125000",
  category: { emoji: "●", id: EXPENSE_CATEGORY_ID, name: "Еда" },
  currency: "RUB",
  deleted_at: null,
  description: "",
  id: "018f0000-0000-7000-8000-000000000030",
  occurred_at: "2026-08-24T09:00:00.000Z",
  source: "manual",
  type: "expense",
  version: 1,
};

const ACTIVE_ACCOUNTS: AccountsResponse = {
  default_account_id: ACTIVE_ACCOUNT_ID,
  items: [
    {
      archived: false,
      currency: "RUB",
      id: ACTIVE_ACCOUNT_ID,
      name: "Основной",
      type: "card",
      version: 1,
    },
  ],
};

const ARCHIVED_ACCOUNTS: AccountsResponse = {
  default_account_id: ACTIVE_ACCOUNT_ID,
  items: [
    {
      archived: true,
      currency: "USD",
      id: ARCHIVED_ACCOUNT_ID,
      name: "Старый долларовый",
      type: "cash",
      version: 2,
    },
  ],
};

function categories(kind: "expense" | "income", archived: boolean): CategoriesResponse {
  return {
    items: archived
      ? []
      : [
          {
            archived: false,
            emoji: kind === "expense" ? "●" : "◆",
            id: kind === "expense" ? EXPENSE_CATEGORY_ID : INCOME_CATEGORY_ID,
            kind,
            name: kind === "expense" ? "Еда" : "Премия",
            version: 1,
          },
        ],
  };
}

function page(nextCursor: string | null, items: Transaction[] = [TRANSACTION]): TransactionPageResponse {
  return { items, next_cursor: nextCursor };
}

function transactionRequests(): string[] {
  return apiGet.mock.calls
    .map((call) => String(call[0]))
    .filter((path) => path.startsWith("/api/v1/transactions?"));
}

function renderPage(): QueryClient {
  const client = new QueryClient({
    defaultOptions: { queries: { gcTime: 60_000, retry: false, staleTime: 0 } },
  });
  const Wrapper = ({ children }: PropsWithChildren) => (
    <QueryClientProvider client={client}>
      <MemoryRouter>{children}</MemoryRouter>
    </QueryClientProvider>
  );
  render(<TransactionsPage />, { wrapper: Wrapper });
  return client;
}

beforeEach(() => {
  apiGet.mockImplementation(async (pathValue: unknown) => {
    const path = String(pathValue);
    if (path === "/api/v1/accounts?archived=false") {
      return ACTIVE_ACCOUNTS;
    }
    if (path === "/api/v1/accounts?archived=true") {
      return ARCHIVED_ACCOUNTS;
    }
    if (path.startsWith("/api/v1/categories?")) {
      const url = new URL(path, "https://numismat.invalid");
      const kind = url.searchParams.get("kind") === "income" ? "income" : "expense";
      return categories(kind, url.searchParams.get("archived") === "true");
    }
    if (path.startsWith("/api/v1/transactions?")) {
      const url = new URL(path, "https://numismat.invalid");
      if (url.searchParams.has("cursor")) {
        return page(null, []);
      }
      return page(url.searchParams.size === 1 ? CURSOR : null);
    }
    throw new Error("Unexpected synthetic API path");
  });
});

afterEach(() => {
  cleanup();
  apiGet.mockReset();
  vi.restoreAllMocks();
});

describe("transaction filters page", () => {
  it("loads catalogs, binds categories to type and restarts pagination from page one", async () => {
    const client = renderPage();
    await screen.findByText("Еда");
    expect(transactionRequests()).toEqual(["/api/v1/transactions?limit=30"]);

    fireEvent.click(screen.getByRole("button", { name: "Показать ещё" }));
    await waitFor(() => {
      expect(transactionRequests().some((path) => path.includes(`cursor=${CURSOR}`))).toBe(true);
    });

    fireEvent.click(screen.getByRole("button", { name: "7 дней" }));
    await waitFor(() => {
      const latest = new URL(transactionRequests().at(-1)!, "https://numismat.invalid");
      expect(latest.searchParams.has("start")).toBe(true);
      expect(latest.searchParams.has("end")).toBe(true);
      expect(latest.searchParams.has("cursor")).toBe(false);
    });

    const type = screen.getByLabelText("Тип операции");
    const category = screen.getByLabelText("Категория");
    expect((category as HTMLSelectElement).disabled).toBe(true);
    fireEvent.change(type, { target: { value: "expense" } });
    await waitFor(() => expect((category as HTMLSelectElement).disabled).toBe(false));
    expect(
      apiGet.mock.calls
        .map((call) => String(call[0]))
        .filter((path) => path.startsWith("/api/v1/categories?")),
    ).toEqual(
      expect.arrayContaining([
        "/api/v1/categories?kind=expense&archived=false",
        "/api/v1/categories?kind=expense&archived=true",
      ]),
    );

    fireEvent.change(screen.getByLabelText("Счёт"), {
      target: { value: ARCHIVED_ACCOUNT_ID },
    });
    fireEvent.change(category, { target: { value: EXPENSE_CATEGORY_ID } });
    fireEvent.change(screen.getByLabelText("Валюта"), { target: { value: "USD" } });
    await waitFor(() => {
      const latest = new URL(transactionRequests().at(-1)!, "https://numismat.invalid");
      expect(Object.fromEntries(latest.searchParams)).toMatchObject({
        limit: "30",
        type: "expense",
        account_id: ARCHIVED_ACCOUNT_ID,
        category_id: EXPENSE_CATEGORY_ID,
        currency: "USD",
      });
      expect(latest.searchParams.has("cursor")).toBe(false);
    });

    fireEvent.change(type, { target: { value: "income" } });
    expect((category as HTMLSelectElement).value).toBe("");
    await screen.findByRole("option", { name: /Премия/u });
    expect(screen.queryByRole("option", { name: /Еда/u })).toBeNull();
    expect(transactionRequests().at(-1)).toContain("type=income");
    expect(transactionRequests().at(-1)).not.toContain("category_id=");

    const unfilteredBeforeReset = transactionRequests().filter(
      (path) => path === "/api/v1/transactions?limit=30",
    ).length;
    fireEvent.click(screen.getByRole("button", { name: "Сбросить" }));
    await waitFor(() => {
      expect(
        transactionRequests().filter((path) => path === "/api/v1/transactions?limit=30"),
      ).toHaveLength(unfilteredBeforeReset + 1);
    });
    expect(client.getQueryCache().findAll({ queryKey: ["transactions", "active"] })).toHaveLength(1);
  });

  it("applies a custom inclusive local-date range without a separate submit click", async () => {
    renderPage();
    await screen.findByText("Еда");

    fireEvent.click(screen.getByRole("button", { name: "30 дней" }));
    await waitFor(() => expect(transactionRequests()).toHaveLength(2));
    const rollingMonthPath = transactionRequests().at(-1);

    fireEvent.click(screen.getByRole("button", { name: "Свой период" }));
    await waitFor(() => expect(transactionRequests()).toHaveLength(3));
    expect(transactionRequests().at(-1)).toBe(rollingMonthPath);

    const start = screen.getByLabelText("С даты");
    const end = screen.getByLabelText("По дату");
    fireEvent.change(start, { target: { value: "2026-08-01" } });
    fireEvent.change(end, { target: { value: "2026-08-10" } });

    await waitFor(() => {
      const latest = new URL(transactionRequests().at(-1)!, "https://numismat.invalid");
      expect(latest.searchParams.get("start")).toBe("2026-07-31T21:00:00.000Z");
      expect(latest.searchParams.get("end")).toBe("2026-08-10T21:00:00.000Z");
      expect(latest.searchParams.has("cursor")).toBe(false);
    });
    expect(screen.getByText("Выбрано: 1")).not.toBeNull();
  });

  it.each([320, 390])(
    "keeps compact accessible stacking semantics at %ipx",
    async (width) => {
      Object.defineProperty(window, "innerWidth", { configurable: true, value: width });
      renderPage();
      await screen.findByText("Еда");

      const panel = document.querySelector(".transaction-filters");
      const grid = document.querySelector(".transaction-filter-grid");
      const body = document.querySelector(".transaction-filters__body");
      expect(panel).not.toBeNull();
      expect(panel?.querySelector("summary")?.textContent).toContain("Фильтры");
      expect(grid?.classList.contains("transaction-filter-grid")).toBe(true);
      expect(grid?.getAttribute("data-layout")).toBe("responsive");
      expect(body?.getAttribute("data-mobile-stack")).toBe("true");
      expect(
        screen.getByRole("group", { name: "Быстрый выбор периода" }).getAttribute(
          "data-mobile-scroll",
        ),
      ).toBe("true");
      expect((screen.getByLabelText("Категория") as HTMLSelectElement).disabled).toBe(true);
      expect(screen.getAllByRole("button", { pressed: false }).length).toBeGreaterThan(0);
      for (const control of document.querySelectorAll(".transaction-filter-control")) {
        expect(control.getAttribute("style")).toBeNull();
      }
    },
  );
});
