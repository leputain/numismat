import { describe, expect, it, vi } from "vitest";

import { SameOriginApiClient } from "./client";
import { MutationResultUnknownError, ReopenRequiredError } from "./errors";

const FIRST_CSRF = "A".repeat(43);
const SECOND_CSRF = `${"B".repeat(42)}E`;

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json; charset=utf-8" },
  });
}

function authenticatedResponse(body: unknown, binding = FIRST_CSRF): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: {
      "Content-Type": "application/json; charset=utf-8",
      "X-Session-Binding": binding,
    },
  });
}

const SESSION_PAYLOAD = {
  authenticated: true,
  base_currency: "RUB",
  expires_at: "2026-08-20T11:00:00.123Z",
  locale: "ru_RU",
  timezone: "Europe/Moscow",
} as const;

function deferred<T>(): {
  readonly promise: Promise<T>;
  readonly resolve: (value: T) => void;
} {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((resolver) => {
    resolve = resolver;
  });
  return { promise, resolve };
}

describe("SameOriginApiClient", () => {
  it("reads bounded JSON when a WebView does not expose a response stream", async () => {
    const response = authenticatedResponse(SESSION_PAYLOAD);
    Object.defineProperty(response, "body", { value: undefined });
    const client = new SameOriginApiClient({ fetch: vi.fn<typeof fetch>().mockResolvedValue(response) });

    await expect(
      client.postAuthentication("/api/v1/auth/telegram", { initData: "opaque" }),
    ).resolves.toEqual(SESSION_PAYLOAD);
  });

  it("reads bounded JSON when a WebView exposes a broken stream reader", async () => {
    const payload = { error: { code: "auth_session_invalid", message: "ignored", details: {} } };
    const response = jsonResponse(payload, 401);
    Object.defineProperty(response, "body", {
      value: {
        getReader(): never {
          throw new TypeError("synthetic broken WebView stream");
        },
      },
    });
    const client = new SameOriginApiClient({ fetch: vi.fn<typeof fetch>().mockResolvedValue(response) });

    await expect(client.get("/api/v1/auth/me", { protected: false, retry: false })).rejects.toMatchObject({
      status: 401,
      code: "auth_session_invalid",
    });
  });

  it("never restores a stale authentication binding after a newer response", async () => {
    const first = deferred<Response>();
    const second = deferred<Response>();
    const newerBinding = `${"B".repeat(42)}E`;
    const fetchMock = vi
      .fn<typeof fetch>()
      .mockImplementationOnce(() => first.promise)
      .mockImplementationOnce(() => second.promise)
      .mockResolvedValueOnce(jsonResponse({ current_period: {} }));
    const client = new SameOriginApiClient({ fetch: fetchMock });

    const staleAuthentication = client.postAuthentication(
      "/api/v1/auth/telegram",
      { initData: "first" },
    );
    const currentAuthentication = client.postAuthentication(
      "/api/v1/auth/telegram",
      { initData: "second" },
    );
    second.resolve(authenticatedResponse(SESSION_PAYLOAD, newerBinding));
    await expect(currentAuthentication).resolves.toEqual(SESSION_PAYLOAD);
    first.resolve(authenticatedResponse(SESSION_PAYLOAD, FIRST_CSRF));
    await expect(staleAuthentication).rejects.toBeInstanceOf(ReopenRequiredError);

    await client.get("/api/v1/dashboard", { retry: false });
    const protectedRequest = fetchMock.mock.calls[2]?.[1];
    expect(new Headers(protectedRequest?.headers).get("X-Session-Binding")).toBe(newerBinding);
  });

  it("uses the fixed same-origin boundary and preserves key across a changed-CSRF retry", async () => {
    const cookieValues = [FIRST_CSRF, SECOND_CSRF];
    const fetchMock = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(authenticatedResponse(SESSION_PAYLOAD))
      .mockResolvedValueOnce(
        jsonResponse({ error: { code: "csrf_failed", message: "ignored", details: {} } }, 403),
      )
      .mockResolvedValueOnce(jsonResponse({ result: { kind: "draft" } }));
    const client = new SameOriginApiClient({
      fetch: fetchMock,
      readCookies: () =>
        `${"unrelated=value; "}__Host-numismat_csrf=${cookieValues.shift() ?? SECOND_CSRF}`,
      randomSource: {
        getRandomValues(array) {
          new Uint8Array(array.buffer, array.byteOffset, array.byteLength).fill(0);
          return array;
        },
      },
    });

    await client.postAuthentication("/api/v1/auth/telegram", { initData: "opaque" });
    const mutation = client.prepareMutation<{ result: { kind: string } }>(
      "/api/v1/drafts/current/confirm",
      { version: 1 },
    );
    await expect(mutation.execute()).resolves.toEqual({ result: { kind: "draft" } });
    expect(fetchMock).toHaveBeenCalledTimes(3);
    const first = fetchMock.mock.calls[1]?.[1];
    const second = fetchMock.mock.calls[2]?.[1];
    expect(first).toMatchObject({
      mode: "same-origin",
      credentials: "same-origin",
      cache: "no-store",
      redirect: "error",
      referrerPolicy: "no-referrer",
      method: "POST",
    });
    expect(new Headers(first?.headers).get("Origin")).toBeNull();
    expect(new Headers(first?.headers).get("Idempotency-Key")).toHaveLength(43);
    expect(new Headers(second?.headers).get("Idempotency-Key")).toBe(
      new Headers(first?.headers).get("Idempotency-Key"),
    );
    expect(new Headers(second?.headers).get("X-CSRF-Token")).toBe(SECOND_CSRF);
    expect(new Headers(first?.headers).get("X-Session-Binding")).toBe(FIRST_CSRF);
    expect(new Headers(second?.headers).get("X-Session-Binding")).toBe(FIRST_CSRF);
  });

  it("does not queue or automatically replay an offline mutation", async () => {
    const fetchMock = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(authenticatedResponse(SESSION_PAYLOAD))
      .mockRejectedValueOnce(new TypeError("offline detail"));
    const client = new SameOriginApiClient({
      fetch: fetchMock,
      readCookies: () => `__Host-numismat_csrf=${FIRST_CSRF}`,
      randomSource: crypto,
    });
    await client.postAuthentication("/api/v1/auth/telegram", { initData: "opaque" });
    const mutation = client.prepareMutation("/api/v1/accounts", { name: "private" });

    await expect(mutation.execute()).rejects.toBeInstanceOf(MutationResultUnknownError);
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("preserves an explicit PATCH method and accepts a successful empty receipt", async () => {
    const fetchMock = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(authenticatedResponse(SESSION_PAYLOAD))
      .mockResolvedValueOnce(new Response(null, { status: 204 }));
    const client = new SameOriginApiClient({
      fetch: fetchMock,
      readCookies: () => `__Host-numismat_csrf=${FIRST_CSRF}`,
      randomSource: crypto,
    });
    await client.postAuthentication("/api/v1/auth/telegram", { initData: "opaque" });
    const mutation = client.prepareMutation<void>(
      "/api/v1/drafts/current",
      { revision: 1, action: "back" },
      "PATCH",
    );

    await expect(mutation.execute()).resolves.toBeUndefined();
    expect(fetchMock.mock.calls[1]?.[1]?.method).toBe("PATCH");
  });
});
