// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import type { PropsWithChildren } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { MutationResultUnknownError } from "../../api/errors";
import type { CategoriesResponse, Category } from "../../shared/api/types";

const apiGet = vi.hoisted(() => vi.fn());
const executeMutation = vi.hoisted(() => vi.fn());
const prepareMutation = vi.hoisted(() => vi.fn(() => ({ execute: executeMutation })));

vi.mock("../../app/providers", () => ({ apiClient: { get: apiGet, prepareMutation } }));

import { CategoriesPage } from "./categories-page";

const EXPENSE_ID = "018f0000-0000-7000-8000-000000000020";
const INCOME_ID = "018f0000-0000-7000-8000-000000000021";
const ARCHIVED_INCOME_ID = "018f0000-0000-7000-8000-000000000022";

function category(
  id: string,
  name: string,
  kind: "expense" | "income",
  archived: boolean,
  version: number,
): Category {
  return { archived, emoji: kind === "expense" ? "●" : "◆", id, kind, name, version };
}

const RESPONSES: Readonly<Record<string, CategoriesResponse>> = {
  "/api/v1/categories?kind=expense&archived=false": {
    items: [category(EXPENSE_ID, "Еда", "expense", false, 4)],
  },
  "/api/v1/categories?kind=expense&archived=true": { items: [] },
  "/api/v1/categories?kind=income&archived=false": {
    items: [category(INCOME_ID, "Премия", "income", false, 6)],
  },
  "/api/v1/categories?kind=income&archived=true": {
    items: [category(ARCHIVED_INCOME_ID, "Старая работа", "income", true, 9)],
  },
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
  render(<CategoriesPage />, { wrapper: Wrapper });
  return client;
}

function row(name: string): HTMLElement {
  const article = screen.getByRole("heading", { name }).closest("article");
  if (article === null) throw new Error(`Category row not found: ${name}`);
  return article;
}

beforeEach(() => {
  apiGet.mockImplementation(async (pathValue: unknown) => {
    const path = String(pathValue);
    const response = RESPONSES[path];
    if (response === undefined) throw new Error(`Unexpected synthetic API path: ${path}`);
    return response;
  });
  executeMutation.mockResolvedValue({
    result: { category_id: EXPENSE_ID, kind: "category", version: 5 },
  });
});

afterEach(() => {
  cleanup();
  apiGet.mockReset();
  executeMutation.mockReset();
  prepareMutation.mockClear();
  vi.restoreAllMocks();
});

describe("categories catalog page", () => {
  it("binds exact reads to kind/archive and prepares exact catalog mutations", async () => {
    renderPage();
    await screen.findByRole("heading", { name: "Еда" });
    expect(apiGet.mock.calls.map((call) => String(call[0]))).toContain(
      "/api/v1/categories?kind=expense&archived=false",
    );

    fireEvent.change(screen.getByLabelText("Название", { selector: "#new-category-name" }), {
      target: { value: "  Обучение  " },
    });
    fireEvent.click(screen.getByRole("button", { name: "Добавить категорию" }));
    await waitFor(() => expect(prepareMutation).toHaveBeenCalledTimes(1));

    fireEvent.click(within(row("Еда")).getByRole("button", { name: "Переименовать" }));
    fireEvent.change(within(row("Еда")).getByLabelText("Название"), {
      target: { value: "  Продукты  " },
    });
    fireEvent.click(within(row("Еда")).getByRole("button", { name: "Сохранить" }));
    await waitFor(() => expect(prepareMutation).toHaveBeenCalledTimes(2));

    fireEvent.click(within(row("Еда")).getByRole("button", { name: "В архив" }));
    fireEvent.click(within(row("Еда")).getByRole("button", { name: "Архивировать" }));
    await waitFor(() => expect(prepareMutation).toHaveBeenCalledTimes(3));

    fireEvent.click(screen.getByRole("tab", { name: "Доходы" }));
    await screen.findByRole("heading", { name: "Премия" });
    expect(apiGet.mock.calls.map((call) => String(call[0]))).toContain(
      "/api/v1/categories?kind=income&archived=false",
    );
    fireEvent.click(screen.getByRole("tab", { name: "Архив" }));
    await screen.findByRole("heading", { name: "Старая работа" });
    expect(apiGet.mock.calls.map((call) => String(call[0]))).toContain(
      "/api/v1/categories?kind=income&archived=true",
    );
    fireEvent.click(within(row("Старая работа")).getByRole("button", { name: "Восстановить" }));
    await waitFor(() => expect(prepareMutation).toHaveBeenCalledTimes(4));

    expect(prepareMutation.mock.calls).toEqual([
      ["/api/v1/categories", { kind: "expense", name: "Обучение" }, "POST"],
      [`/api/v1/categories/${EXPENSE_ID}`, { name: "Продукты", version: 4 }, "PATCH"],
      [`/api/v1/categories/${EXPENSE_ID}/archive`, { version: 4 }, "POST"],
      [`/api/v1/categories/${ARCHIVED_INCOME_ID}/restore`, { version: 9 }, "POST"],
    ]);
  });

  it("keeps one prepared category mutation for a safe unknown-outcome retry", async () => {
    executeMutation
      .mockRejectedValueOnce(new MutationResultUnknownError())
      .mockResolvedValueOnce({
        result: { category_id: EXPENSE_ID, kind: "category", version: 5 },
      });
    renderPage();
    await screen.findByRole("heading", { name: "Еда" });

    fireEvent.click(within(row("Еда")).getByRole("button", { name: "В архив" }));
    fireEvent.click(within(row("Еда")).getByRole("button", { name: "Архивировать" }));
    expect(await screen.findByText("Результат пока не подтверждён")).not.toBeNull();
    expect(prepareMutation).toHaveBeenCalledOnce();
    expect((within(row("Еда")).getByRole("button", { name: "Архивировать" }) as HTMLButtonElement).disabled).toBe(true);

    fireEvent.click(screen.getByRole("button", { name: "Повторить безопасно" }));
    await waitFor(() => expect(executeMutation).toHaveBeenCalledTimes(2));
    expect(prepareMutation).toHaveBeenCalledOnce();
    expect(prepareMutation).toHaveBeenCalledWith(
      `/api/v1/categories/${EXPENSE_ID}/archive`,
      { version: 4 },
      "POST",
    );
  });

  it.each([320, 390])("keeps accessible stacked controls at %ipx", async (width) => {
    Object.defineProperty(window, "innerWidth", { configurable: true, value: width });
    renderPage();
    await screen.findByRole("heading", { name: "Еда" });

    expect(screen.getByRole("tablist", { name: "Тип категорий" })).not.toBeNull();
    expect(screen.getByRole("tablist", { name: "Состояние категорий" })).not.toBeNull();
    const createForm = screen.getByRole("button", { name: "Добавить категорию" }).closest("form");
    expect(createForm?.className).toContain("space-y-4");
    expect(createForm?.querySelector(".field-stack")).not.toBeNull();
    expect(row("Еда").querySelector(".flex.flex-wrap")?.getAttribute("style")).toBeNull();
  });
});
