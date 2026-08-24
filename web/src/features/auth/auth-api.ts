import type { components } from "../../api/generated/schema";
import { ProtocolError } from "../../api/errors";
import type { SameOriginApiClient } from "../../api/client";

type GeneratedAuthSession = components["schemas"]["AuthSessionResponse"];

export interface AuthSession {
  readonly locale: string;
  readonly timezone: string;
  readonly baseCurrency: string;
  readonly settingsVersion: number;
  readonly expiresAt: string;
}

export interface AuthApi {
  getSession(options?: { readonly retry?: boolean }): Promise<AuthSession>;
  authenticate(rawInitData: string): Promise<AuthSession>;
  logout(): Promise<void>;
}

function parseSession(payload: GeneratedAuthSession): AuthSession {
  if (
    typeof payload !== "object" ||
    payload === null ||
    payload.authenticated !== true ||
    typeof payload.locale !== "string" ||
    payload.locale.length < 1 ||
    payload.locale.length > 32 ||
    typeof payload.timezone !== "string" ||
    payload.timezone.length < 1 ||
    payload.timezone.length > 64 ||
    typeof payload.base_currency !== "string" ||
    !/^[A-Z]{3}$/u.test(payload.base_currency) ||
    typeof payload.settings_version !== "number" ||
    !Number.isInteger(payload.settings_version) ||
    payload.settings_version < 1 ||
    payload.settings_version > 2 ** 31 - 1 ||
    typeof payload.expires_at !== "string" ||
    !Number.isFinite(Date.parse(payload.expires_at))
  ) {
    throw new ProtocolError();
  }
  return {
    locale: payload.locale,
    timezone: payload.timezone,
    baseCurrency: payload.base_currency,
    settingsVersion: payload.settings_version,
    expiresAt: payload.expires_at,
  };
}

export function createAuthApi(client: SameOriginApiClient): AuthApi {
  return {
    async getSession(options = {}): Promise<AuthSession> {
      const payload = await client.get<GeneratedAuthSession>("/api/v1/auth/me", {
        ...(options.retry === undefined ? {} : { retry: options.retry }),
      });
      return parseSession(payload);
    },
    async authenticate(rawInitData: string): Promise<AuthSession> {
      const payload = await client.postAuthentication<GeneratedAuthSession>(
        "/api/v1/auth/telegram",
        { initData: rawInitData },
      );
      return parseSession(payload);
    },
    logout: () => client.logout(),
  };
}
