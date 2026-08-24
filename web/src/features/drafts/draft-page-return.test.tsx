// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router";
import type { PropsWithChildren } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { Draft } from "../../shared/api/types";

const apiGet = vi.hoisted(() => vi.fn());
const executeMutation = vi.hoisted(() => vi.fn());
const prepareMutation = vi.hoisted(() => vi.fn(() => ({ execute: executeMutation })));

vi.mock("../../app/providers", () => ({ apiClient: { get: apiGet, prepareMutation } }));
vi.mock("../auth/auth-context", () => ({
  useAuth: () => ({ closeMiniApp: vi.fn() }),
}));
vi.mock("../../shared/auth/use-session-format", () => ({
  useSessionFormat: () => ({
    baseCurrency: "RUB",
    locale: "ru-RU",
    timeZone: "Europe/Moscow",
  }),
}));
vi.mock("./draft-state-renderer", () => ({
  DraftStateRenderer: () => <div>Текущий шаг</div>,
}));

import { DraftPage } from "./draft-page";

const DRAFT_ID = "018f0000-0000-7000-8000-000000000030";
const BATCH_ID = "018f0000-0000-7000-8000-000000000031";
const SCHEDULE_ID = "018f0000-0000-7000-8000-000000000032";

const ACTIVE_DRAFT: Draft = {
  conflict: null,
  edit_target: null,
  flow: "quick",
  id: DRAFT_ID,
  revision: 12,
  rule: null,
  state: "review",
  supported: true,
  suspended: false,
  transaction: null,
};

function renderPage(initialEntry: string) {
  const client = new QueryClient({
    defaultOptions: {
      mutations: { retry: false },
      queries: { gcTime: 60_000, retry: false, staleTime: 0 },
    },
  });
  const Wrapper = ({ children }: PropsWithChildren) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
  render(
    <MemoryRouter initialEntries={[initialEntry]}>
      <Routes>
        <Route element={<DraftPage />} path="/draft" />
        <Route element={<div>Импортный пакет</div>} path="/imports/:batchId" />
        <Route element={<div>Регулярная операция</div>} path="/recurring/:scheduleId" />
      </Routes>
    </MemoryRouter>,
    { wrapper: Wrapper },
  );
}

beforeEach(() => {
  apiGet.mockImplementation(async (pathValue: unknown) => {
    const path = String(pathValue);
    if (path === "/api/v1/drafts/active") return { draft: ACTIVE_DRAFT };
    throw new Error(`Unexpected synthetic API path: ${path}`);
  });
  executeMutation.mockResolvedValue(undefined);
});

afterEach(() => {
  cleanup();
  apiGet.mockReset();
  executeMutation.mockReset();
  prepareMutation.mockClear();
  vi.restoreAllMocks();
});

describe("draft terminal Back return", () => {
  it.each([
    [`/draft?returnKind=bank_import&batchId=${BATCH_ID}`, "Импортный пакет"],
    [`/draft?returnKind=recurring&scheduleId=${SCHEDULE_ID}`, "Регулярная операция"],
  ])("returns from %s when Back closes the draft with 204", async (entry, targetTitle) => {
    renderPage(entry);
    fireEvent.click(await screen.findByRole("button", { name: "Назад" }));

    await screen.findByText(targetTitle);
    expect(prepareMutation).toHaveBeenCalledOnce();
    expect(prepareMutation).toHaveBeenCalledWith(
      `/api/v1/drafts/${DRAFT_ID}`,
      { action: "back", revision: 12 },
      "PATCH",
    );
  });

  it("does not leave the draft when a non-terminal Back returns the next projection", async () => {
    executeMutation.mockResolvedValue({
      result: { draft_id: DRAFT_ID, kind: "draft", revision: 13 },
    });
    renderPage(`/draft?returnKind=bank_import&batchId=${BATCH_ID}`);
    fireEvent.click(await screen.findByRole("button", { name: "Назад" }));

    await waitFor(() => expect(executeMutation).toHaveBeenCalledOnce());
    expect(screen.queryByText("Импортный пакет")).toBeNull();
    expect(screen.getByText("Текущий шаг")).not.toBeNull();
  });
});
