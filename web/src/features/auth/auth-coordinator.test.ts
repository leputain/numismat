import { describe, expect, it, vi } from "vitest";

import type { TelegramMiniAppPort } from "../../adapters/telegram/telegram-web-app.types";
import { HttpApiError, NetworkError, ReopenRequiredError } from "../../api/errors";
import type { AuthApi, AuthSession } from "./auth-api";
import { AuthCoordinator } from "./auth-coordinator";

const SESSION: AuthSession = {
  locale: "ru",
  timezone: "Europe/Moscow",
  baseCurrency: "RUB",
  expiresAt: "2030-01-01T00:00:00Z",
};

const RESTORED_SESSION: AuthSession = {
  ...SESSION,
  locale: "en",
};

function deferred<T>() {
  let resolve!: (value: T | PromiseLike<T>) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((promiseResolve, promiseReject) => {
    resolve = promiseResolve;
    reject = promiseReject;
  });
  return { promise, reject, resolve };
}

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

function coordinator(api: AuthApi, clear = vi.fn(), clearView = vi.fn()) {
  return {
    clear,
    clearView,
    value: new AuthCoordinator({
      api,
      telegram: telegram(),
      clearProtectedView: clearView,
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
          getSession: vi.fn(),
          authenticate: vi.fn<AuthApi["authenticate"]>().mockResolvedValue(SESSION),
          logout: vi.fn(),
        },
        telegram: telegram(),
        clearProtectedView: vi.fn(),
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

  it("binds every concurrent launch through one signed Telegram proof before trusting cookies", async () => {
    const getSession = vi.fn<AuthApi["getSession"]>();
    const authenticate = vi.fn<AuthApi["authenticate"]>().mockResolvedValue(SESSION);
    const value = coordinator({ getSession, authenticate, logout: vi.fn() }).value;

    const [first, second] = await Promise.all([value.start(), value.start()]);
    expect(first.status).toBe("authenticated");
    expect(second.status).toBe("authenticated");
    expect(getSession).not.toHaveBeenCalled();
    expect(authenticate).toHaveBeenCalledTimes(1);
    expect(authenticate).toHaveBeenCalledWith("raw-proof");
  });

  it("repeats the exact signed POST once after an unknown result and never falls back to /me", async () => {
    const ambiguousMe = vi.fn<AuthApi["getSession"]>();
    const ambiguousAuth = vi
      .fn<AuthApi["authenticate"]>()
      .mockRejectedValueOnce(new NetworkError())
      .mockResolvedValueOnce(SESSION);
    const ambiguous = coordinator({
      getSession: ambiguousMe,
      authenticate: ambiguousAuth,
      logout: vi.fn(),
    });
    await expect(ambiguous.value.start()).resolves.toMatchObject({ status: "authenticated" });
    expect(ambiguousAuth).toHaveBeenCalledTimes(2);
    expect(ambiguousAuth.mock.calls).toEqual([["raw-proof"], ["raw-proof"]]);
    expect(ambiguousMe).not.toHaveBeenCalled();

    const replay = coordinator({
      getSession: vi.fn(),
      authenticate: vi
        .fn<AuthApi["authenticate"]>()
        .mockRejectedValue(new HttpApiError(409, "telegram_auth_replayed")),
      logout: vi.fn(),
    });
    await expect(replay.value.start()).resolves.toMatchObject({ status: "reopen_required" });
    expect(replay.clear).toHaveBeenCalledTimes(2);
    ambiguous.value.handleProtectedUnauthorized();
    expect(ambiguous.clear).toHaveBeenCalledTimes(2);
    expect(ambiguous.value.state.status).toBe("reopen_required");
    expect(replay.value.state.status).toBe("reopen_required");
  });

  it("preserves the page binding while hidden and restores only through /me", async () => {
    const authenticate = vi.fn<AuthApi["authenticate"]>().mockResolvedValue(SESSION);
    const getSession = vi.fn<AuthApi["getSession"]>().mockResolvedValue(RESTORED_SESSION);
    const value = coordinator(
      { getSession, authenticate, logout: vi.fn() },
      vi.fn(),
      vi.fn(),
    );

    await expect(value.value.start()).resolves.toMatchObject({ status: "authenticated" });
    value.value.suspendProtectedSession();
    expect(value.value.state.status).toBe("booting");
    await expect(value.value.recheckAuthenticatedSession()).resolves.toMatchObject({
      status: "authenticated",
      session: RESTORED_SESSION,
    });

    expect(authenticate).toHaveBeenCalledTimes(1);
    expect(getSession).toHaveBeenCalledOnce();
    expect(getSession).toHaveBeenCalledWith({ retry: false });
    expect(value.clear).toHaveBeenCalledTimes(1);
    expect(value.clearView).toHaveBeenCalledTimes(1);
  });

  it("requires reopen when the preserved binding no longer matches the shared cookie", async () => {
    const authenticate = vi.fn<AuthApi["authenticate"]>().mockResolvedValue(SESSION);
    const getSession = vi
      .fn<AuthApi["getSession"]>()
      .mockRejectedValue(new HttpApiError(401, "auth_session_invalid"));
    const value = coordinator({ getSession, authenticate, logout: vi.fn() });

    await expect(value.value.start()).resolves.toMatchObject({ status: "authenticated" });
    value.value.suspendProtectedSession();
    await expect(value.value.recheckAuthenticatedSession()).resolves.toMatchObject({
      status: "reopen_required",
    });

    expect(getSession).toHaveBeenCalledWith({ retry: false });
    expect(authenticate).toHaveBeenCalledOnce();
    expect(value.clearView).toHaveBeenCalledOnce();
    expect(value.clear).toHaveBeenCalledTimes(2);
  });

  it("invalidates the client auth attempt when hidden during signed authentication", async () => {
    const pendingAuthentication = deferred<AuthSession>();
    const authenticate = vi
      .fn<AuthApi["authenticate"]>()
      .mockImplementation(() => pendingAuthentication.promise);
    const getSession = vi
      .fn<AuthApi["getSession"]>()
      .mockRejectedValue(new ReopenRequiredError());
    const value = coordinator({ getSession, authenticate, logout: vi.fn() });

    const initialRun = value.value.start();
    expect(value.value.state.status).toBe("authenticating_telegram");

    value.value.suspendProtectedSession();
    expect(value.value.state.status).toBe("booting");
    expect(value.clear).toHaveBeenCalledTimes(2);
    expect(value.clearView).not.toHaveBeenCalled();

    pendingAuthentication.resolve(SESSION);
    await initialRun;
    expect(value.value.state.status).toBe("booting");

    await expect(value.value.recheckAuthenticatedSession()).resolves.toMatchObject({
      status: "reopen_required",
    });
    expect(authenticate).toHaveBeenCalledOnce();
    expect(getSession).toHaveBeenCalledOnce();
  });

  it("ignores stale /me results across hidden-page generations", async () => {
    const firstCheck = deferred<AuthSession>();
    const secondCheck = deferred<AuthSession>();
    const getSession = vi
      .fn<AuthApi["getSession"]>()
      .mockImplementationOnce(() => firstCheck.promise)
      .mockImplementationOnce(() => secondCheck.promise);
    const authenticate = vi.fn<AuthApi["authenticate"]>().mockResolvedValue(SESSION);
    const readRawInitDataForAuthentication = vi
      .fn<TelegramMiniAppPort["readRawInitDataForAuthentication"]>()
      .mockReturnValue("proof-one");
    const clearProtectedState = vi.fn();
    const clearProtectedView = vi.fn();
    const value = new AuthCoordinator({
      api: { getSession, authenticate, logout: vi.fn() },
      telegram: { ...telegram(), readRawInitDataForAuthentication },
      clearProtectedView,
      clearProtectedState,
      now: () => Date.parse("2029-01-01T00:00:00Z"),
      setTimer: () => 1 as ReturnType<typeof setTimeout>,
      clearTimer: () => undefined,
    });

    await expect(value.start()).resolves.toMatchObject({ status: "authenticated" });

    value.suspendProtectedSession();
    expect(value.state.status).toBe("booting");
    const firstRun = value.recheckAuthenticatedSession();

    value.suspendProtectedSession();
    expect(value.state.status).toBe("booting");
    const secondRun = value.recheckAuthenticatedSession();

    firstCheck.resolve(SESSION);
    await firstRun;
    expect(value.state.status).toBe("booting");

    secondCheck.reject(new ReopenRequiredError());
    await expect(secondRun).resolves.toMatchObject({
      status: "reopen_required",
    });
    expect(authenticate).toHaveBeenCalledOnce();
    expect(readRawInitDataForAuthentication).toHaveBeenCalledOnce();
    expect(getSession).toHaveBeenCalledTimes(2);
    expect(clearProtectedState).toHaveBeenCalledTimes(3);
    expect(clearProtectedView).toHaveBeenCalledOnce();
  });
});
