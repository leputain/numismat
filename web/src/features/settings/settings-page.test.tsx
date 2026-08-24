// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { PropsWithChildren } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { MutationResultUnknownError } from "../../api/errors";

const executeMutation = vi.hoisted(() => vi.fn());
const prepareMutation = vi.hoisted(() => vi.fn(() => ({ execute: executeMutation })));
const refreshSession = vi.hoisted(() => vi.fn());

vi.mock("../../app/providers", () => ({ apiClient: { prepareMutation } }));
vi.mock("./notification-preferences-card", () => ({
  NotificationPreferencesCard: () => null,
}));
vi.mock("../auth/auth-context", () => ({
  useAuth: () => ({
    refreshSession,
    state: {
      status: "authenticated",
      session: {
        locale: "ru",
        timezone: "Europe/Moscow",
        baseCurrency: "RUB",
        settingsVersion: 7,
        expiresAt: "2030-01-01T00:00:00Z",
      },
    },
  }),
}));

import { canonicalIanaTimezone, SettingsPage } from "./settings-page";

function renderPage() {
  const client = new QueryClient({
    defaultOptions: { mutations: { retry: false }, queries: { retry: false } },
  });
  const Wrapper = ({ children }: PropsWithChildren) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
  render(<SettingsPage />, { wrapper: Wrapper });
}

function refreshed(timezone: string, settingsVersion: number) {
  return {
    status: "authenticated" as const,
    session: {
      locale: "ru",
      timezone,
      baseCurrency: "RUB",
      settingsVersion,
      expiresAt: "2030-01-01T00:00:00Z",
    },
  };
}

beforeEach(() => {
  executeMutation.mockResolvedValue(undefined);
});

afterEach(() => {
  cleanup();
  executeMutation.mockReset();
  prepareMutation.mockClear();
  refreshSession.mockReset();
  vi.restoreAllMocks();
});

describe("settings page", () => {
  it("shows immutable currency and sends an exact optimistic timezone mutation", async () => {
    refreshSession.mockResolvedValue(refreshed("Europe/Samara", 8));
    renderPage();

    expect(screen.getByText("Базовая валюта")).not.toBeNull();
    expect(screen.getByText("RUB")).not.toBeNull();
    expect(screen.getByDisplayValue("Europe/Moscow")).not.toBeNull();

    fireEvent.change(screen.getByLabelText("IANA timezone"), {
      target: { value: "Europe/Samara" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Сохранить" }));

    await waitFor(() => expect(executeMutation).toHaveBeenCalledOnce());
    expect(prepareMutation).toHaveBeenCalledWith(
      "/api/v1/settings/timezone",
      { timezone: "Europe/Samara", version: 7 },
      "PUT",
    );
    await waitFor(() => expect(refreshSession).toHaveBeenCalledOnce());
    expect(await screen.findByText(/данные сессии обновлены/u)).not.toBeNull();
  });

  it("retains one prepared request for an unknown outcome and retries it safely", async () => {
    executeMutation
      .mockRejectedValueOnce(new MutationResultUnknownError())
      .mockResolvedValueOnce(undefined);
    refreshSession
      .mockResolvedValueOnce(refreshed("Europe/Moscow", 7))
      .mockResolvedValueOnce(refreshed("Asia/Omsk", 8));
    renderPage();

    fireEvent.change(screen.getByLabelText("IANA timezone"), {
      target: { value: "Asia/Omsk" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Сохранить" }));
    expect(await screen.findByText("Результат пока не подтверждён")).not.toBeNull();
    expect(prepareMutation).toHaveBeenCalledOnce();

    fireEvent.click(screen.getByRole("button", { name: "Повторить безопасно" }));
    await waitFor(() => expect(executeMutation).toHaveBeenCalledTimes(2));
    expect(prepareMutation).toHaveBeenCalledOnce();
    await waitFor(() => expect(refreshSession).toHaveBeenCalledTimes(2));
  });

  it("does not report success from a stale refresh and can confirm by rereading", async () => {
    refreshSession
      .mockResolvedValueOnce(refreshed("Europe/Moscow", 7))
      .mockResolvedValueOnce(refreshed("Europe/Samara", 8));
    renderPage();

    fireEvent.change(screen.getByLabelText("IANA timezone"), {
      target: { value: "Europe/Samara" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Сохранить" }));

    expect(await screen.findByText(/чтение настроек — нет/u)).not.toBeNull();
    expect(screen.queryByText(/данные сессии обновлены/u)).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Перечитать профиль" }));
    expect(await screen.findByText(/данные сессии обновлены/u)).not.toBeNull();
  });

  it("rejects unknown or oversized zones before preparing a request", () => {
    renderPage();
    fireEvent.change(screen.getByLabelText("IANA timezone"), {
      target: { value: "Not/AZone" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Сохранить" }));

    expect(screen.getByRole("alert").textContent).toContain("IANA timezone");
    expect(prepareMutation).not.toHaveBeenCalled();
    expect(canonicalIanaTimezone("x".repeat(65))).toBeUndefined();
  });
});
