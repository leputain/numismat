import { describe, expect, it, vi } from "vitest";

import { SameOriginApiClient } from "./client";
import { MutationResultUnknownError } from "./errors";

const FIRST_CSRF = "A".repeat(43);
const SECOND_CSRF = `${"B".repeat(42)}E`;

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json; charset=utf-8" },
  });
}

describe("SameOriginApiClient", () => {
  it("uses the fixed same-origin boundary and preserves key across a changed-CSRF retry", async () => {
    const cookieValues = [FIRST_CSRF, SECOND_CSRF];
    const fetchMock = vi
      .fn<typeof fetch>()
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

    const mutation = client.prepareMutation<{ result: { kind: string } }>(
      "/api/v1/drafts/current/confirm",
      { version: 1 },
    );
    await expect(mutation.execute()).resolves.toEqual({ result: { kind: "draft" } });
    expect(fetchMock).toHaveBeenCalledTimes(2);
    const first = fetchMock.mock.calls[0]?.[1];
    const second = fetchMock.mock.calls[1]?.[1];
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
  });

  it("does not queue or automatically replay an offline mutation", async () => {
    const fetchMock = vi.fn<typeof fetch>().mockRejectedValue(new TypeError("offline detail"));
    const client = new SameOriginApiClient({
      fetch: fetchMock,
      readCookies: () => `__Host-numismat_csrf=${FIRST_CSRF}`,
      randomSource: crypto,
    });
    const mutation = client.prepareMutation("/api/v1/accounts", { name: "private" });

    await expect(mutation.execute()).rejects.toBeInstanceOf(MutationResultUnknownError);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("preserves an explicit PATCH method and accepts a successful empty receipt", async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(new Response(null, { status: 204 }));
    const client = new SameOriginApiClient({
      fetch: fetchMock,
      readCookies: () => `__Host-numismat_csrf=${FIRST_CSRF}`,
      randomSource: crypto,
    });
    const mutation = client.prepareMutation<void>(
      "/api/v1/drafts/current",
      { revision: 1, action: "back" },
      "PATCH",
    );

    await expect(mutation.execute()).resolves.toBeUndefined();
    expect(fetchMock.mock.calls[0]?.[1]?.method).toBe("PATCH");
  });
});
