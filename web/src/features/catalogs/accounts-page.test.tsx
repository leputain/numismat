// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import type { PropsWithChildren } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { MutationResultUnknownError } from "../../api/errors";
import type { AccountsResponse } from "../../shared/api/types";

const apiGet = vi.hoisted(() => vi.fn());
const executeMutation = vi.hoisted(() => vi.fn());
const prepareMutation = vi.hoisted(() => vi.fn(() => ({ execute: executeMutation })));

vi.mock("../../app/providers", () => ({ apiClient: { get: apiGet, prepareMutation } }));

import { AccountsPage } from "./accounts-page";

const MAIN_ID = "018f0000-0000-7000-8000-000000000010";
const RESERVE_ID = "018f0000-0000-7000-8000-000000000011";
const ARCHIVED_ID = "018f0000-0000-7000-8000-000000000012";

const ACTIVE_ACCOUNTS: AccountsResponse = {
  default_account_id: MAIN_ID,
  items: [
    {
      archived: false,
      currency: "RUB",
      id: MAIN_ID,
      name: "Основной",
      type: "card",
      version: 3,
    },
    {
      archived: false,
      currency: "USD",
      id: RESERVE_ID,
      name: "Резерв",
      type: "cash",
      version: 7,
    },
  ],
};

const ARCHIVED_ACCOUNTS: AccountsResponse = {
  default_account_id: MAIN_ID,
  items: [
    {
      archived: true,
      currency: "EUR",
      id: ARCHIVED_ID,
      name: "Старый счёт",
      type: "card",
      version: 11,
    },
  ],
};

function renderPage(): QueryClient {
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
  render(<AccountsPage />, { wrapper: Wrapper });
  return client;
}

function row(name: string): HTMLElement {
  const article = screen.getByRole("heading", { name }).closest("article");
  if (article === null) throw new Error(`Account row not found: ${name}`);
  return article;
}

beforeEach(() => {
  apiGet.mockImplementation(async (pathValue: unknown) => {
    const path = String(pathValue);
    if (path === "/api/v1/accounts?archived=false") return ACTIVE_ACCOUNTS;
    if (path === "/api/v1/accounts?archived=true") return ARCHIVED_ACCOUNTS;
    throw new Error(`Unexpected synthetic API path: ${path}`);
  });
  executeMutation.mockResolvedValue({
    result: { account_id: RESERVE_ID, kind: "account", version: 8 },
  });
});

afterEach(() => {
  cleanup();
  apiGet.mockReset();
  executeMutation.mockReset();
  prepareMutation.mockClear();
  vi.restoreAllMocks();
});

describe("accounts catalog page", () => {
  it("uses exact catalog reads and prepares create, rename and versioned actions", async () => {
    renderPage();
    await screen.findByRole("heading", { name: "Резерв" });
    expect(apiGet.mock.calls.map((call) => String(call[0]))).toContain(
      "/api/v1/accounts?archived=false",
    );

    fireEvent.change(screen.getByLabelText("Название", { selector: "#new-account-name" }), {
      target: { value: "  Копилка  " },
    });
    fireEvent.change(screen.getByLabelText("Валюта"), { target: { value: "usd" } });
    fireEvent.click(screen.getByRole("button", { name: "Добавить счёт" }));
    await waitFor(() => expect(prepareMutation).toHaveBeenCalledTimes(1));

    const reserve = row("Резерв");
    fireEvent.click(within(reserve).getByRole("button", { name: "Переименовать" }));
    fireEvent.change(within(reserve).getByLabelText("Название"), {
      target: { value: "  Резервный  " },
    });
    fireEvent.click(within(reserve).getByRole("button", { name: "Сохранить" }));
    await waitFor(() => expect(prepareMutation).toHaveBeenCalledTimes(2));

    fireEvent.click(within(row("Резерв")).getByRole("button", { name: "Сделать основным" }));
    await waitFor(() => expect(prepareMutation).toHaveBeenCalledTimes(3));

    const mainArchive = within(row("Основной")).getByRole("button", { name: "В архив" });
    expect((mainArchive as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(mainArchive);
    expect(screen.queryByText("Убрать счёт в архив?")).toBeNull();

    fireEvent.click(within(row("Резерв")).getByRole("button", { name: "В архив" }));
    fireEvent.click(within(row("Резерв")).getByRole("button", { name: "Архивировать" }));
    await waitFor(() => expect(prepareMutation).toHaveBeenCalledTimes(4));

    fireEvent.click(screen.getByRole("tab", { name: "Архив" }));
    await screen.findByRole("heading", { name: "Старый счёт" });
    expect(apiGet.mock.calls.map((call) => String(call[0]))).toContain(
      "/api/v1/accounts?archived=true",
    );
    fireEvent.click(within(row("Старый счёт")).getByRole("button", { name: "Восстановить" }));
    await waitFor(() => expect(prepareMutation).toHaveBeenCalledTimes(5));

    expect(prepareMutation.mock.calls).toEqual([
      ["/api/v1/accounts", { currency: "USD", name: "Копилка" }, "POST"],
      [`/api/v1/accounts/${RESERVE_ID}`, { name: "Резервный", version: 7 }, "PATCH"],
      [`/api/v1/accounts/${RESERVE_ID}/default`, { version: 7 }, "POST"],
      [`/api/v1/accounts/${RESERVE_ID}/archive`, { version: 7 }, "POST"],
      [`/api/v1/accounts/${ARCHIVED_ID}/restore`, { version: 11 }, "POST"],
    ]);
  });

  it("keeps one prepared account mutation for a safe unknown-outcome retry", async () => {
    executeMutation
      .mockRejectedValueOnce(new MutationResultUnknownError())
      .mockResolvedValueOnce({
        result: { account_id: RESERVE_ID, kind: "account", version: 8 },
      });
    renderPage();
    await screen.findByRole("heading", { name: "Резерв" });

    fireEvent.click(within(row("Резерв")).getByRole("button", { name: "Сделать основным" }));
    expect(await screen.findByText("Результат пока не подтверждён")).not.toBeNull();
    expect(prepareMutation).toHaveBeenCalledOnce();
    expect((within(row("Резерв")).getByRole("button", { name: "Переименовать" }) as HTMLButtonElement).disabled).toBe(true);

    fireEvent.click(screen.getByRole("button", { name: "Повторить безопасно" }));
    await waitFor(() => expect(executeMutation).toHaveBeenCalledTimes(2));
    expect(prepareMutation).toHaveBeenCalledOnce();
    expect(prepareMutation).toHaveBeenCalledWith(
      `/api/v1/accounts/${RESERVE_ID}/default`,
      { version: 7 },
      "POST",
    );
  });

  it.each([320, 390])("keeps accessible stacked controls at %ipx", async (width) => {
    Object.defineProperty(window, "innerWidth", { configurable: true, value: width });
    renderPage();
    await screen.findByRole("heading", { name: "Резерв" });

    expect(screen.getByRole("tablist", { name: "Состояние счетов" })).not.toBeNull();
    const createForm = screen.getByRole("button", { name: "Добавить счёт" }).closest("form");
    expect(createForm?.querySelector(".grid")?.className).toContain("sm:grid-cols");
    expect(createForm?.querySelectorAll(".field-stack")).toHaveLength(2);
    expect(within(row("Резерв")).getAllByRole("button")).toHaveLength(3);
    expect(row("Резерв").querySelector(".flex.flex-wrap")?.getAttribute("style")).toBeNull();
  });
});
