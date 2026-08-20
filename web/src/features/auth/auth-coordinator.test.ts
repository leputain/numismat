import { describe, expect, it, vi } from "vitest";

import type { TelegramMiniAppPort } from "../../adapters/telegram/telegram-web-app.types";
import { HttpApiError, NetworkError } from "../../api/errors";
import type { AuthApi, AuthSession } from "./auth-api";
import { AuthCoordinator } from "./auth-coordinator";

const SESSION: AuthSession = {
  locale: "ru",
  timezone: "Europe/Moscow",
  baseCurrency: "RUB",
  expiresAt: "2030-01-01T00:00:00Z",
};

function telegram(): TelegramMiniAppPort {
  return {
    initialize: vi.fn(),
    readRawInitDataForAuthentication: vi.fn(() => "raw-proof"),
    subscribeTheme: () => () => undefined,
    subscribeViewport: () => () => undefined,
    setBackButton: vi.fn(),
    close: vi.fn(),
    dispose: vi.fn(),
  };
}

function coordinator(api: AuthApi, clear = vi.fn()) {
  return {
    clear,
    value: new AuthCoordinator({
      api,
      telegram: telegram(),
      clearProtectedState: clear,
      now: () => Date.parse("2029-01-01T00:00:00Z"),
      setTimer: () => 1 as ReturnType<typeof setTimeout>,
      clearTimer: () => undefined,
    }),
  };
}

describe("AuthCoordinator", () => {
  it("calls native browser timers with the global receiver", async () => {
    const timer = 1 as ReturnType<typeof setTimeout>;
    const setTimer = vi.fn(function (this: typeof globalThis) {
      if (this !== globalThis) {
        throw new TypeError("invalid native timer receiver");
      }
      return timer;
    });
    const clearTimer = vi.fn(function (this: typeof globalThis) {
      if (this !== globalThis) {
        throw new TypeError("invalid native timer receiver");
      }
    });
    vi.stubGlobal("setTimeout", setTimer);
    vi.stubGlobal("clearTimeout", clearTimer);

    try {
      const value = new AuthCoordinator({
        api: {
          getSession: vi.fn<AuthApi["getSession"]>().mockResolvedValue(SESSION),
          authenticate: vi.fn(),
          logout: vi.fn(),
        },
        telegram: telegram(),
        clearProtectedState: vi.fn(),
        now: () => Date.parse("2029-01-01T00:00:00Z"),
      });

      await expect(value.start()).resolves.toMatchObject({ status: "authenticated" });
      value.dispose();
      expect(setTimer).toHaveBeenCalledTimes(1);
      expect(clearTimer).toHaveBeenCalledWith(timer);
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it("checks /me first and spends at most one Telegram proof under concurrent start", async () => {
    const getSession = vi
      .fn<AuthApi["getSession"]>()
      .mockRejectedValueOnce(new HttpApiError(401, "auth_session_invalid"));
    const authenticate = vi.fn<AuthApi["authenticate"]>().mockResolvedValue(SESSION);
    const value = coordinator({ getSession, authenticate, logout: vi.fn() }).value;

    const [first, second] = await Promise.all([value.start(), value.start()]);
    expect(first.status).toBe("authenticated");
    expect(second.status).toBe("authenticated");
    expect(getSession).toHaveBeenCalledTimes(1);
    expect(authenticate).toHaveBeenCalledTimes(1);
  });

  it("resolves ambiguous auth through one /me and never replays after replay or session 401", async () => {
    const ambiguousMe = vi
      .fn<AuthApi["getSession"]>()
      .mockRejectedValueOnce(new HttpApiError(401, "auth_session_invalid"))
      .mockResolvedValueOnce(SESSION);
    const ambiguousAuth = vi
      .fn<AuthApi["authenticate"]>()
      .mockRejectedValueOnce(new NetworkError());
    const ambiguous = coordinator({
      getSession: ambiguousMe,
      authenticate: ambiguousAuth,
      logout: vi.fn(),
    });
    await expect(ambiguous.value.start()).resolves.toMatchObject({ status: "authenticated" });
    expect(ambiguousAuth).toHaveBeenCalledTimes(1);
    expect(ambiguousMe).toHaveBeenLastCalledWith({ retry: false });

    const replay = coordinator({
      getSession: vi
        .fn<AuthApi["getSession"]>()
        .mockRejectedValue(new HttpApiError(401, "auth_session_invalid")),
      authenticate: vi
        .fn<AuthApi["authenticate"]>()
        .mockRejectedValue(new HttpApiError(409, "telegram_auth_replayed")),
      logout: vi.fn(),
    });
    await expect(replay.value.start()).resolves.toMatchObject({ status: "reopen_required" });
    expect(replay.clear).toHaveBeenCalledTimes(1);
    ambiguous.value.handleProtectedUnauthorized();
    expect(ambiguous.clear).toHaveBeenCalledTimes(1);
    expect(ambiguous.value.state.status).toBe("reopen_required");
    expect(replay.value.state.status).toBe("reopen_required");
  });
});
