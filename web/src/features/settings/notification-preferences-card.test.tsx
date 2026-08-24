// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { PropsWithChildren } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { HttpApiError, MutationResultUnknownError } from "../../api/errors";
import { queryKeys } from "../../shared/queries/query-keys";

const get = vi.hoisted(() => vi.fn());
const executeMutation = vi.hoisted(() => vi.fn());
const prepareMutation = vi.hoisted(() => vi.fn(() => ({ execute: executeMutation })));

vi.mock("../../app/providers", () => ({ apiClient: { get, prepareMutation } }));

import { NotificationPreferencesCard } from "./notification-preferences-card";
import type { NotificationPreferencesResponse } from "./notification-preferences-contract";

const DEFAULTS: NotificationPreferencesResponse = {
  budget_80_enabled: false,
  budget_100_enabled: false,
  recurring_ready_enabled: false,
  weekly_digest_enabled: false,
  quiet_start: null,
  quiet_end: null,
  weekly_weekday: 0,
  weekly_time: "09:00",
  version: 0,
};

function renderCard() {
  const client = new QueryClient({
    defaultOptions: { mutations: { retry: false }, queries: { retry: false } },
  });
  const Wrapper = ({ children }: PropsWithChildren) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
  render(<NotificationPreferencesCard />, { wrapper: Wrapper });
  return client;
}

function editedSnapshot(version = 1): NotificationPreferencesResponse {
  return {
    budget_80_enabled: true,
    budget_100_enabled: false,
    recurring_ready_enabled: false,
    weekly_digest_enabled: true,
    quiet_start: "23:00",
    quiet_end: "06:30",
    weekly_weekday: 4,
    weekly_time: "18:30",
    version,
  };
}

async function fillEditedPreferences() {
  fireEvent.click(await screen.findByRole("switch", { name: /раннее предупреждение/u }));
  fireEvent.click(screen.getByRole("switch", { name: /Еженедельная сводка/u }));
  fireEvent.change(screen.getByLabelText("День сводки"), { target: { value: "4" } });
  fireEvent.change(screen.getByLabelText("Время"), { target: { value: "18:30" } });
  fireEvent.click(screen.getByRole("switch", { name: /Тихие часы/u }));
  fireEvent.change(screen.getByLabelText("С"), { target: { value: "23:00" } });
  fireEvent.change(screen.getByLabelText("До"), { target: { value: "06:30" } });
}

beforeEach(() => {
  get.mockResolvedValue(DEFAULTS);
  executeMutation.mockResolvedValue(undefined);
});

afterEach(() => {
  cleanup();
  get.mockReset();
  executeMutation.mockReset();
  prepareMutation.mockClear();
  vi.restoreAllMocks();
});

describe("notification preferences", () => {
  it("loads the fail-safe default opt-outs", async () => {
    renderCard();

    const switches = await screen.findAllByRole("switch");
    expect(switches).toHaveLength(5);
    expect(switches.every((item) => !(item as HTMLInputElement).checked)).toBe(true);
    expect((screen.getByRole("button", { name: "Без изменений" }) as HTMLButtonElement).disabled)
      .toBe(true);
  });

  it("sends the exact optimistic replace and confirms it from the server", async () => {
    get.mockResolvedValueOnce(DEFAULTS).mockResolvedValueOnce(editedSnapshot());
    renderCard();
    await fillEditedPreferences();
    fireEvent.click(screen.getByRole("button", { name: "Сохранить уведомления" }));

    await waitFor(() => expect(executeMutation).toHaveBeenCalledOnce());
    expect(prepareMutation).toHaveBeenCalledWith(
      "/api/v1/settings/notifications",
      {
        budget_80_enabled: true,
        budget_100_enabled: false,
        recurring_ready_enabled: false,
        weekly_digest_enabled: true,
        quiet_start: "23:00",
        quiet_end: "06:30",
        weekly_weekday: 4,
        weekly_time: "18:30",
        version: 0,
      },
      "PUT",
    );
    expect(await screen.findByText(/сохранены и перечитаны/u)).not.toBeNull();
  });

  it("fails closed on an invalid quiet-hours pair", async () => {
    renderCard();
    fireEvent.click(await screen.findByRole("switch", { name: /Тихие часы/u }));
    fireEvent.change(screen.getByLabelText("С"), { target: { value: "22:00" } });
    fireEvent.change(screen.getByLabelText("До"), { target: { value: "22:00" } });
    fireEvent.click(screen.getByRole("button", { name: "Сохранить уведомления" }));

    expect(screen.getByRole("alert").textContent).toContain("начало и конец");
    expect(prepareMutation).not.toHaveBeenCalled();
  });

  it("retries an unknown outcome with the same prepared request", async () => {
    let server = DEFAULTS;
    get.mockImplementation(async () => server);
    executeMutation
      .mockRejectedValueOnce(new MutationResultUnknownError())
      .mockImplementationOnce(async () => {
        server = editedSnapshot();
      });
    renderCard();
    await fillEditedPreferences();
    fireEvent.click(screen.getByRole("button", { name: "Сохранить уведомления" }));

    expect(await screen.findByText("Результат пока не подтверждён")).not.toBeNull();
    expect(prepareMutation).toHaveBeenCalledOnce();
    fireEvent.click(screen.getByRole("button", { name: "Повторить безопасно" }));

    await waitFor(() => expect(executeMutation).toHaveBeenCalledTimes(2));
    expect(prepareMutation).toHaveBeenCalledOnce();
    expect(await screen.findByText(/сохранены и перечитаны/u)).not.toBeNull();
  });

  it("does not report success when the post-write refetch is stale", async () => {
    get
      .mockResolvedValueOnce(DEFAULTS)
      .mockResolvedValueOnce(DEFAULTS)
      .mockResolvedValueOnce(editedSnapshot());
    renderCard();
    await fillEditedPreferences();
    fireEvent.click(screen.getByRole("button", { name: "Сохранить уведомления" }));

    expect(await screen.findByText("Нужно перечитать настройки")).not.toBeNull();
    expect(screen.queryByText(/сохранены и перечитаны/u)).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Перечитать" }));
    expect(await screen.findByText(/сохранены и перечитаны/u)).not.toBeNull();
  });

  it("refreshes the canonical version after an optimistic conflict", async () => {
    const concurrent = { ...DEFAULTS, budget_100_enabled: true, version: 1 };
    get.mockResolvedValueOnce(DEFAULTS).mockResolvedValueOnce(concurrent);
    executeMutation.mockRejectedValueOnce(new HttpApiError(409, "object_version_conflict"));
    renderCard();
    fireEvent.click(await screen.findByRole("switch", { name: /раннее предупреждение/u }));
    fireEvent.click(screen.getByRole("button", { name: "Сохранить уведомления" }));

    expect(await screen.findByText(/изменились в другом окне/u)).not.toBeNull();
    await waitFor(() =>
      expect(
        (screen.getByRole("switch", { name: /лимит достигнут/u }) as HTMLInputElement).checked,
      ).toBe(true),
    );
    expect(get).toHaveBeenCalledTimes(2);
  });

  it("never advances a dirty editor to a background-refetched version", async () => {
    let server = DEFAULTS;
    get.mockImplementation(async () => server);
    const client = renderCard();
    fireEvent.click(await screen.findByRole("switch", { name: /раннее предупреждение/u }));

    server = { ...DEFAULTS, budget_100_enabled: true, version: 1 };
    await client.invalidateQueries({ queryKey: queryKeys.settings.notificationPreferences });
    await waitFor(() => expect(get).toHaveBeenCalledTimes(2));
    fireEvent.click(screen.getByRole("button", { name: "Сохранить уведомления" }));

    await waitFor(() => expect(prepareMutation).toHaveBeenCalledOnce());
    expect(prepareMutation).toHaveBeenCalledWith(
      "/api/v1/settings/notifications",
      expect.objectContaining({
        budget_80_enabled: true,
        budget_100_enabled: false,
        version: 0,
      }),
      "PUT",
    );
  });

  it("rejects a malformed GET payload instead of enabling controls", async () => {
    get.mockResolvedValue({ ...DEFAULTS, budget_80_enabled: "true" });
    renderCard();

    expect(await screen.findByText("Не удалось загрузить данные")).not.toBeNull();
    expect(screen.queryByRole("switch")).toBeNull();
  });

  it.each([320, 390])("keeps switches and time fields accessible at %ipx", async (width) => {
    Object.defineProperty(window, "innerWidth", { configurable: true, value: width });
    renderCard();

    fireEvent.click(await screen.findByRole("switch", { name: /Еженедельная сводка/u }));
    fireEvent.click(screen.getByRole("switch", { name: /Тихие часы/u }));
    expect((screen.getByLabelText("Время") as HTMLInputElement).disabled).toBe(false);
    expect((screen.getByLabelText("С") as HTMLInputElement).disabled).toBe(false);
    expect((screen.getByLabelText("До") as HTMLInputElement).disabled).toBe(false);
  });
});
