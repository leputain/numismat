import type { TelegramMiniAppPort } from "../../adapters/telegram/telegram-web-app.types";
import { TelegramSdkUnavailableError } from "../../adapters/telegram/telegram-web-app";
import { HttpApiError, NetworkError } from "../../api/errors";
import { emitClientEvent } from "../../shared/logging/client-events";
import type { AuthApi, AuthSession } from "./auth-api";

export type AuthState =
  | { readonly status: "booting" }
  | { readonly status: "sdk_unavailable" }
  | { readonly status: "checking_session" }
  | { readonly status: "session_check_retryable" }
  | { readonly status: "authenticating_telegram" }
  | { readonly status: "auth_result_unknown" }
  | { readonly status: "authenticated"; readonly session: AuthSession }
  | { readonly status: "reopen_required" }
  | { readonly status: "access_denied" }
  | { readonly status: "deployment_error" }
  | { readonly status: "signed_out" }
  | { readonly status: "protocol_error" };

interface AuthCoordinatorDependencies {
  readonly api: AuthApi;
  readonly telegram: TelegramMiniAppPort;
  readonly clearProtectedState: () => void;
  readonly now?: () => number;
  readonly setTimer?: (callback: () => void, delay: number) => ReturnType<typeof setTimeout>;
  readonly clearTimer?: (timer: ReturnType<typeof setTimeout>) => void;
}

type AuthStateListener = (state: AuthState) => void;

export class AuthCoordinator {
  readonly #api: AuthApi;
  readonly #telegram: TelegramMiniAppPort;
  readonly #clearProtectedState: () => void;
  readonly #now: () => number;
  readonly #setTimer: (callback: () => void, delay: number) => ReturnType<typeof setTimeout>;
  readonly #clearTimer: (timer: ReturnType<typeof setTimeout>) => void;
  readonly #listeners = new Set<AuthStateListener>();
  #state: AuthState = { status: "booting" };
  #authAttempted = false;
  #startPromise: Promise<AuthState> | undefined;
  #telegramAuthPromise: Promise<AuthState> | undefined;
  #expiryTimer: ReturnType<typeof setTimeout> | undefined;

  public constructor(dependencies: AuthCoordinatorDependencies) {
    this.#api = dependencies.api;
    this.#telegram = dependencies.telegram;
    this.#clearProtectedState = dependencies.clearProtectedState;
    this.#now = dependencies.now ?? Date.now;
    this.#setTimer = dependencies.setTimer ?? setTimeout;
    this.#clearTimer = dependencies.clearTimer ?? clearTimeout;
  }

  public get state(): AuthState {
    return this.#state;
  }

  public subscribe(listener: AuthStateListener): () => void {
    this.#listeners.add(listener);
    return () => this.#listeners.delete(listener);
  }

  public start(): Promise<AuthState> {
    if (this.#startPromise !== undefined) {
      return this.#startPromise;
    }
    if (this.#state.status !== "booting") {
      return Promise.resolve(this.#state);
    }
    this.#startPromise = this.#startLifecycle();
    return this.#startPromise;
  }

  public async retrySessionCheck(): Promise<AuthState> {
    if (this.#state.status !== "session_check_retryable") {
      return this.#state;
    }
    return this.#checkSession();
  }

  public async recheckAuthenticatedSession(): Promise<AuthState> {
    if (this.#state.status !== "authenticated") {
      return this.#state;
    }
    try {
      const session = await this.#api.getSession();
      if (this.#state.status === "authenticated") {
        this.#authenticate(session);
      }
    } catch (error) {
      if (error instanceof HttpApiError && error.status === 401) {
        this.handleProtectedUnauthorized();
      } else if (!(error instanceof NetworkError)) {
        this.#fatal("protocol_error");
      }
    }
    return this.#state;
  }

  public handleProtectedUnauthorized(): void {
    this.#terminal("reopen_required");
  }

  public async logout(): Promise<AuthState> {
    try {
      await this.#api.logout();
    } catch (error) {
      if (!(error instanceof HttpApiError) || error.status !== 401) {
        throw error;
      }
    }
    this.#clearProtectedState();
    this.#clearExpiryTimer();
    this.#transition({ status: "signed_out" });
    return this.#state;
  }

  public closeMiniApp(): void {
    this.#telegram.close();
  }

  public dispose(): void {
    this.#clearExpiryTimer();
    this.#listeners.clear();
    this.#telegram.dispose();
  }

  async #startLifecycle(): Promise<AuthState> {
    try {
      this.#telegram.initialize();
    } catch (error) {
      if (error instanceof TelegramSdkUnavailableError) {
        this.#transition({ status: "sdk_unavailable" });
        return this.#state;
      }
      this.#fatal("protocol_error");
      return this.#state;
    }
    return this.#checkSession();
  }

  async #checkSession(): Promise<AuthState> {
    this.#transition({ status: "checking_session" });
    try {
      const session = await this.#api.getSession();
      this.#authenticate(session);
      return this.#state;
    } catch (error) {
      if (error instanceof HttpApiError && error.status === 401) {
        if (this.#authAttempted) {
          this.#terminal("reopen_required");
          return this.#state;
        }
        return this.#authenticateWithTelegram();
      }
      if (error instanceof NetworkError || (error instanceof HttpApiError && error.status >= 500)) {
        this.#transition({ status: "session_check_retryable" });
        return this.#state;
      }
      this.#fatal("protocol_error");
      return this.#state;
    }
  }

  async #authenticateWithTelegram(): Promise<AuthState> {
    if (this.#telegramAuthPromise !== undefined) {
      return this.#telegramAuthPromise;
    }
    this.#telegramAuthPromise = this.#performTelegramAuthentication();
    return this.#telegramAuthPromise;
  }

  async #performTelegramAuthentication(): Promise<AuthState> {
    if (this.#authAttempted) {
      this.#terminal("reopen_required");
      return this.#state;
    }
    this.#authAttempted = true;
    this.#transition({ status: "authenticating_telegram" });

    let rawInitData: string;
    try {
      rawInitData = this.#telegram.readRawInitDataForAuthentication();
    } catch {
      this.#transition({ status: "sdk_unavailable" });
      return this.#state;
    }

    try {
      const session = await this.#api.authenticate(rawInitData);
      this.#authenticate(session);
      return this.#state;
    } catch (error) {
      if (error instanceof NetworkError || (error instanceof HttpApiError && error.status >= 500)) {
        return this.#resolveAmbiguousAuthentication();
      }
      if (error instanceof HttpApiError) {
        if (
          error.status === 401 ||
          (error.status === 409 && error.code === "telegram_auth_replayed")
        ) {
          this.#terminal("reopen_required");
          return this.#state;
        }
        if (error.status === 403 && error.code === "telegram_owner_forbidden") {
          this.#terminal("access_denied");
          return this.#state;
        }
        if (error.status === 403 && error.code === "origin_forbidden") {
          this.#terminal("deployment_error");
          return this.#state;
        }
      }
      this.#fatal("protocol_error");
      return this.#state;
    } finally {
      rawInitData = "";
    }
  }

  async #resolveAmbiguousAuthentication(): Promise<AuthState> {
    this.#transition({ status: "auth_result_unknown" });
    try {
      const session = await this.#api.getSession({ retry: false });
      this.#authenticate(session);
    } catch {
      this.#terminal("reopen_required");
    }
    return this.#state;
  }

  #authenticate(session: AuthSession): void {
    const expiresAt = Date.parse(session.expiresAt);
    if (!Number.isFinite(expiresAt)) {
      this.#fatal("protocol_error");
      return;
    }
    this.#clearExpiryTimer();
    const delay = Math.max(0, Math.min(expiresAt - this.#now(), 2_147_483_647));
    this.#expiryTimer = this.#setTimer(() => {
      this.#terminal("reopen_required");
    }, delay);
    this.#transition({ status: "authenticated", session });
  }

  #fatal(status: "protocol_error"): void {
    this.#clearProtectedState();
    this.#clearExpiryTimer();
    emitClientEvent("auth_protocol_error");
    this.#transition({ status });
  }

  #terminal(status: "reopen_required" | "access_denied" | "deployment_error"): void {
    this.#clearProtectedState();
    this.#clearExpiryTimer();
    if (status === "reopen_required") {
      emitClientEvent("auth_reopen_required");
    }
    this.#transition({ status });
  }

  #clearExpiryTimer(): void {
    if (this.#expiryTimer !== undefined) {
      this.#clearTimer(this.#expiryTimer);
      this.#expiryTimer = undefined;
    }
  }

  #transition(state: AuthState): void {
    this.#state = state;
    for (const listener of this.#listeners) {
      listener(state);
    }
  }
}
