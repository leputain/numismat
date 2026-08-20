import type { TelegramMiniAppPort } from "../../adapters/telegram/telegram-web-app.types";
import { TelegramSdkUnavailableError } from "../../adapters/telegram/telegram-web-app";
import { HttpApiError, NetworkError, ReopenRequiredError } from "../../api/errors";
import { emitClientEvent } from "../../shared/logging/client-events";
import type { AuthApi, AuthSession } from "./auth-api";

export type AuthState =
  | { readonly status: "booting" }
  | { readonly status: "sdk_unavailable" }
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
  readonly clearProtectedView: () => void;
  readonly clearProtectedState: () => void;
  readonly now?: () => number;
  readonly setTimer?: (callback: () => void, delay: number) => ReturnType<typeof setTimeout>;
  readonly clearTimer?: (timer: ReturnType<typeof setTimeout>) => void;
}

type AuthStateListener = (state: AuthState) => void;

export class AuthCoordinator {
  readonly #api: AuthApi;
  readonly #telegram: TelegramMiniAppPort;
  readonly #clearProtectedView: () => void;
  readonly #clearProtectedState: () => void;
  readonly #now: () => number;
  readonly #setTimer: (callback: () => void, delay: number) => ReturnType<typeof setTimeout>;
  readonly #clearTimer: (timer: ReturnType<typeof setTimeout>) => void;
  readonly #listeners = new Set<AuthStateListener>();
  #state: AuthState = { status: "booting" };
  #authGeneration = 0;
  #startPromise: Promise<AuthState> | undefined;
  #sessionCheckPromise: Promise<AuthState> | undefined;
  #telegramAuthPromise: Promise<AuthState> | undefined;
  #expiryTimer: ReturnType<typeof setTimeout> | undefined;

  public constructor(dependencies: AuthCoordinatorDependencies) {
    this.#api = dependencies.api;
    this.#telegram = dependencies.telegram;
    this.#clearProtectedView = dependencies.clearProtectedView;
    this.#clearProtectedState = dependencies.clearProtectedState;
    const now = dependencies.now;
    const setTimer = dependencies.setTimer;
    const clearTimer = dependencies.clearTimer;
    this.#now = now === undefined ? () => Date.now() : () => now();
    this.#setTimer =
      setTimer === undefined
        ? (callback, delay) => globalThis.setTimeout(callback, delay)
        : (callback, delay) => setTimer(callback, delay);
    this.#clearTimer =
      clearTimer === undefined
        ? (timer) => globalThis.clearTimeout(timer)
        : (timer) => clearTimer(timer);
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
    this.#startPromise = this.#startLifecycle(this.#authGeneration);
    return this.#startPromise;
  }

  public async recheckAuthenticatedSession(): Promise<AuthState> {
    if (this.#state.status !== "authenticated" && this.#state.status !== "booting") {
      return this.#state;
    }
    if (this.#sessionCheckPromise !== undefined) {
      return this.#sessionCheckPromise;
    }
    if (this.#state.status === "authenticated") {
      this.#clearProtectedView();
      this.#clearExpiryTimer();
      this.#transition({ status: "booting" });
    }
    const sessionCheck = this.#performSessionRecheck(this.#authGeneration);
    this.#sessionCheckPromise = sessionCheck;
    try {
      return await sessionCheck;
    } finally {
      if (this.#sessionCheckPromise === sessionCheck) {
        this.#sessionCheckPromise = undefined;
      }
    }
  }

  public suspendProtectedSession(): void {
    const canPreserveSessionBinding = this.#state.status === "authenticated";
    this.#invalidateAuthGeneration();
    if (canPreserveSessionBinding) {
      this.#clearProtectedView();
    } else {
      this.#clearProtectedState();
    }
    this.#clearExpiryTimer();
    this.#transition({ status: "booting" });
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
    this.#invalidateAuthGeneration();
    this.#clearExpiryTimer();
    this.#listeners.clear();
    this.#telegram.dispose();
  }

  async #startLifecycle(generation: number): Promise<AuthState> {
    try {
      this.#telegram.initialize();
    } catch (error) {
      if (!this.#isCurrentAuthGeneration(generation)) {
        return this.#state;
      }
      if (error instanceof TelegramSdkUnavailableError) {
        this.#transition({ status: "sdk_unavailable" });
        return this.#state;
      }
      this.#fatal("protocol_error", generation);
      return this.#state;
    }
    if (!this.#isCurrentAuthGeneration(generation)) {
      return this.#state;
    }
    this.#clearProtectedState();
    return this.#authenticateWithTelegram(generation);
  }

  async #performSessionRecheck(generation: number): Promise<AuthState> {
    if (!this.#isCurrentAuthGeneration(generation)) {
      return this.#state;
    }
    try {
      const session = await this.#api.getSession({ retry: false });
      this.#authenticate(session, generation);
    } catch (error) {
      if (!this.#isCurrentAuthGeneration(generation)) {
        return this.#state;
      }
      if (this.#state.status === "reopen_required") {
        return this.#state;
      }
      if (
        error instanceof ReopenRequiredError ||
        error instanceof NetworkError ||
        (error instanceof HttpApiError && (error.status === 401 || error.status >= 500))
      ) {
        this.#terminal("reopen_required", generation);
      } else {
        this.#fatal("protocol_error", generation);
      }
    }
    return this.#state;
  }

  async #authenticateWithTelegram(generation: number): Promise<AuthState> {
    if (!this.#isCurrentAuthGeneration(generation)) {
      return this.#state;
    }
    if (this.#telegramAuthPromise !== undefined) {
      return this.#telegramAuthPromise;
    }
    const authentication = this.#performTelegramAuthentication(generation);
    this.#telegramAuthPromise = authentication;
    try {
      return await authentication;
    } finally {
      if (this.#telegramAuthPromise === authentication) {
        this.#telegramAuthPromise = undefined;
      }
    }
  }

  async #performTelegramAuthentication(generation: number): Promise<AuthState> {
    if (!this.#isCurrentAuthGeneration(generation)) {
      return this.#state;
    }
    this.#transition({ status: "authenticating_telegram" });

    let rawInitData: string;
    try {
      rawInitData = this.#telegram.readRawInitDataForAuthentication();
    } catch {
      if (this.#isCurrentAuthGeneration(generation)) {
        this.#transition({ status: "sdk_unavailable" });
      }
      return this.#state;
    }

    try {
      const session = await this.#api.authenticate(rawInitData);
      this.#authenticate(session, generation);
      return this.#state;
    } catch (error) {
      if (!this.#isCurrentAuthGeneration(generation)) {
        return this.#state;
      }
      if (error instanceof NetworkError || (error instanceof HttpApiError && error.status >= 500)) {
        return this.#resolveAmbiguousAuthentication(rawInitData, generation);
      }
      return this.#handleAuthenticationFailure(error, generation);
    } finally {
      rawInitData = "";
    }
  }

  async #resolveAmbiguousAuthentication(
    rawInitData: string,
    generation: number,
  ): Promise<AuthState> {
    if (!this.#isCurrentAuthGeneration(generation)) {
      return this.#state;
    }
    this.#transition({ status: "auth_result_unknown" });
    try {
      const session = await this.#api.authenticate(rawInitData);
      this.#authenticate(session, generation);
    } catch (error) {
      if (!this.#isCurrentAuthGeneration(generation)) {
        return this.#state;
      }
      if (error instanceof NetworkError || (error instanceof HttpApiError && error.status >= 500)) {
        this.#terminal("reopen_required", generation);
      } else {
        this.#handleAuthenticationFailure(error, generation);
      }
    }
    return this.#state;
  }

  #handleAuthenticationFailure(error: unknown, generation: number): AuthState {
    if (!this.#isCurrentAuthGeneration(generation)) {
      return this.#state;
    }
    if (error instanceof HttpApiError) {
      if (
        error.status === 401 ||
        (error.status === 409 && error.code === "telegram_auth_replayed")
      ) {
        this.#terminal("reopen_required", generation);
        return this.#state;
      }
      if (error.status === 403 && error.code === "telegram_owner_forbidden") {
        this.#terminal("access_denied", generation);
        return this.#state;
      }
      if (error.status === 403 && error.code === "origin_forbidden") {
        this.#terminal("deployment_error", generation);
        return this.#state;
      }
    }
    this.#fatal("protocol_error", generation);
    return this.#state;
  }

  #authenticate(session: AuthSession, generation: number): void {
    if (!this.#isCurrentAuthGeneration(generation)) {
      return;
    }
    const expiresAt = Date.parse(session.expiresAt);
    if (!Number.isFinite(expiresAt)) {
      this.#fatal("protocol_error", generation);
      return;
    }
    this.#clearExpiryTimer();
    const delay = Math.max(0, Math.min(expiresAt - this.#now(), 2_147_483_647));
    this.#expiryTimer = this.#setTimer(() => {
      this.#terminal("reopen_required", generation);
    }, delay);
    this.#transition({ status: "authenticated", session });
  }

  #fatal(status: "protocol_error", generation = this.#authGeneration): void {
    if (!this.#isCurrentAuthGeneration(generation)) {
      return;
    }
    this.#clearProtectedState();
    this.#clearExpiryTimer();
    emitClientEvent("auth_protocol_error");
    this.#transition({ status });
  }

  #terminal(
    status: "reopen_required" | "access_denied" | "deployment_error",
    generation = this.#authGeneration,
  ): void {
    if (!this.#isCurrentAuthGeneration(generation)) {
      return;
    }
    this.#clearProtectedState();
    this.#clearExpiryTimer();
    if (status === "reopen_required") {
      emitClientEvent("auth_reopen_required");
    }
    this.#transition({ status });
  }

  #invalidateAuthGeneration(): void {
    this.#authGeneration += 1;
    this.#startPromise = undefined;
    this.#sessionCheckPromise = undefined;
    this.#telegramAuthPromise = undefined;
  }

  #isCurrentAuthGeneration(generation: number): boolean {
    return generation === this.#authGeneration;
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
