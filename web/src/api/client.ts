import {
  HttpApiError,
  MutationResultUnknownError,
  NetworkError,
  ProtocolError,
  ReopenRequiredError,
  normalizeApiErrorCode,
} from "./errors";
import { readExactCsrfCookie } from "./cookies";
import { createIdempotencyKey } from "./idempotency";
import type { RandomSource } from "./idempotency";

type FetchImplementation = (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>;

interface ClientDependencies {
  readonly fetch?: FetchImplementation;
  readonly readCookies?: () => string;
  readonly randomSource?: RandomSource;
  readonly onProtectedUnauthorized?: () => void;
}

interface GetOptions {
  readonly signal?: AbortSignal;
  readonly protected?: boolean;
  readonly retry?: boolean;
}

const JSON_HEADERS = {
  Accept: "application/json",
  "Content-Type": "application/json",
} as const;
const MAX_JSON_REQUEST_BYTES = 12 * 1024;
const MAX_JSON_RESPONSE_BYTES = 2 * 1024 * 1024;
const MAX_BANK_IMPORT_BYTES = 2 * 1024 * 1024;
const CANONICAL_UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/u;

const BASE_REQUEST: Readonly<RequestInit> = {
  mode: "same-origin",
  credentials: "same-origin",
  cache: "no-store",
  redirect: "error",
  referrerPolicy: "no-referrer",
};

function assertApiPath(path: string): void {
  if (
    !path.startsWith("/api/v1/") ||
    path.includes("\\") ||
    path.includes("#") ||
    /[\u0000-\u0020\u007f]/u.test(path)
  ) {
    throw new ProtocolError();
  }
  const parsed = new URL(path, "https://numismat.invalid");
  if (parsed.origin !== "https://numismat.invalid" || !parsed.pathname.startsWith("/api/v1/")) {
    throw new ProtocolError();
  }
}

function assertBankImportUploadPath(path: string): void {
  assertApiPath(path);
  const parsed = new URL(path, "https://numismat.invalid");
  const keys = Array.from(parsed.searchParams.keys()).sort();
  const accountId = parsed.searchParams.get("account_id");
  const accountVersion = parsed.searchParams.get("account_version");
  if (
    parsed.pathname !== "/api/v1/bank-imports/upload" ||
    keys.join(",") !== "account_id,account_version,profile" ||
    accountId === null ||
    !CANONICAL_UUID.test(accountId) ||
    accountVersion === null ||
    !/^[1-9][0-9]{0,9}$/u.test(accountVersion) ||
    Number(accountVersion) > 2 ** 31 - 1 ||
    parsed.searchParams.get("profile") !== "canonical_v1"
  ) {
    throw new ProtocolError();
  }
}

function isJsonResponse(response: Response): boolean {
  const contentType = response.headers.get("content-type");
  return contentType !== null && /^application\/json(?:\s*;|$)/iu.test(contentType);
}

function isErrorEnvelope(value: unknown): value is { error: { code: unknown; message: string } } {
  if (typeof value !== "object" || value === null || !("error" in value)) {
    return false;
  }
  const error = value.error;
  if (
    typeof error === "object" &&
    error !== null &&
    "code" in error &&
    "message" in error &&
    typeof error.code === "string" &&
    typeof error.message === "string"
  ) {
    const topLevelKeys = Object.keys(value);
    const errorKeys = Object.keys(error);
    if (
      topLevelKeys.length !== 1 ||
      errorKeys.some((key) => key !== "code" && key !== "message" && key !== "details")
    ) {
      return false;
    }
    if (!("details" in error)) {
      return true;
    }
    const details = error.details;
    return (
      typeof details === "object" &&
      details !== null &&
      !Array.isArray(details) &&
      Object.values(details).every((detail) => typeof detail === "number" && Number.isFinite(detail))
    );
  }
  return false;
}

function assertBoundedJsonRequest(serialized: string): void {
  if (new TextEncoder().encode(serialized).byteLength > MAX_JSON_REQUEST_BYTES) {
    throw new ProtocolError();
  }
}

async function readBoundedJson(response: Response): Promise<unknown> {
  const contentLength = response.headers.get("content-length");
  if (contentLength !== null) {
    if (!/^\d+$/u.test(contentLength) || Number(contentLength) > MAX_JSON_RESPONSE_BYTES) {
      await response.body?.cancel().catch(() => undefined);
      throw new ProtocolError();
    }
  }
  if (response.body === null) {
    throw new ProtocolError();
  }

  const reader = response.body.getReader();
  const chunks: Uint8Array[] = [];
  let size = 0;
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) {
        break;
      }
      size += value.byteLength;
      if (size > MAX_JSON_RESPONSE_BYTES) {
        await reader.cancel();
        throw new ProtocolError();
      }
      chunks.push(value);
    }
  } catch (error) {
    if (error instanceof ProtocolError) {
      throw error;
    }
    throw new ProtocolError();
  }

  const bytes = new Uint8Array(size);
  let offset = 0;
  for (const chunk of chunks) {
    bytes.set(chunk, offset);
    offset += chunk.byteLength;
  }
  try {
    const text = new TextDecoder("utf-8", { fatal: true }).decode(bytes);
    return JSON.parse(text) as unknown;
  } catch {
    throw new ProtocolError();
  }
}

async function parseJsonResponse<T>(response: Response): Promise<T> {
  if (response.redirected || response.type === "opaqueredirect" || !isJsonResponse(response)) {
    throw new ProtocolError();
  }

  const payload = await readBoundedJson(response);

  if (response.ok) {
    return payload as T;
  }
  if (!isErrorEnvelope(payload)) {
    throw new ProtocolError();
  }
  throw new HttpApiError(response.status, normalizeApiErrorCode(payload.error.code));
}

export interface PreparedMutation<TResponse> {
  /** Reuses the same in-memory body and idempotency key after an ambiguous result. */
  execute(signal?: AbortSignal): Promise<TResponse>;
}

export interface PreparedRawMutation<TResponse> {
  /** Reuses one copied opaque body and idempotency key after an ambiguous result. */
  execute(signal?: AbortSignal): Promise<TResponse>;
}

export type RawMutationContentType =
  | "text/csv; charset=utf-8"
  | "text/csv; charset=windows-1251";

export type MutationMethod = "PATCH" | "POST" | "PUT";

export class SameOriginApiClient {
  readonly #fetch: FetchImplementation;
  readonly #readCookies: () => string;
  readonly #randomSource: RandomSource;
  readonly #onProtectedUnauthorized: () => void;

  public constructor(dependencies: ClientDependencies = {}) {
    this.#fetch = dependencies.fetch ?? globalThis.fetch.bind(globalThis);
    this.#readCookies =
      dependencies.readCookies ?? (() => (typeof document === "undefined" ? "" : document.cookie));
    this.#randomSource = dependencies.randomSource ?? globalThis.crypto;
    this.#onProtectedUnauthorized = dependencies.onProtectedUnauthorized ?? (() => undefined);
  }

  public async get<T>(path: string, options: GetOptions = {}): Promise<T> {
    assertApiPath(path);
    const attempts = options.retry === false ? 1 : 2;
    let response: Response | undefined;
    for (let attempt = 0; attempt < attempts; attempt += 1) {
      try {
        response = await this.#fetchGet(path, options.signal);
        if (response.status < 500 || attempt === attempts - 1) {
          break;
        }
        await response.body?.cancel().catch(() => undefined);
      } catch {
        if (attempt === attempts - 1) {
          throw new NetworkError();
        }
      }
    }
    if (response === undefined) {
      throw new NetworkError();
    }

    if (response.status === 401 && options.protected !== false) {
      this.#onProtectedUnauthorized();
    }
    return parseJsonResponse<T>(response);
  }

  public async postAuthentication<T>(path: string, body: unknown): Promise<T> {
    assertApiPath(path);
    let serialized: string;
    try {
      const candidate = JSON.stringify(body);
      if (typeof candidate !== "string") {
        throw new ProtocolError();
      }
      assertBoundedJsonRequest(candidate);
      serialized = candidate;
    } catch {
      throw new ProtocolError();
    }
    let response: Response;
    try {
      response = await this.#fetch(path, {
        ...BASE_REQUEST,
        method: "POST",
        headers: JSON_HEADERS,
        body: serialized,
      });
    } catch {
      throw new NetworkError();
    }
    if (response.status >= 500) {
      await response.body?.cancel().catch(() => undefined);
      throw new NetworkError();
    }
    return parseJsonResponse<T>(response);
  }

  public prepareMutation<TResponse>(
    path: string,
    body: unknown,
    method: MutationMethod = "POST",
  ): PreparedMutation<TResponse> {
    assertApiPath(path);
    let serialized: string;
    try {
      const candidate = JSON.stringify(body);
      if (typeof candidate !== "string") {
        throw new ProtocolError();
      }
      assertBoundedJsonRequest(candidate);
      serialized = candidate;
    } catch {
      throw new ProtocolError();
    }
    const idempotencyKey = createIdempotencyKey(this.#randomSource);
    return {
      execute: (signal?: AbortSignal) =>
        this.#executeMutation<TResponse>(path, method, serialized, idempotencyKey, signal),
    };
  }

  public prepareRawMutation<TResponse>(
    path: string,
    content: Uint8Array,
    contentType: RawMutationContentType,
  ): PreparedRawMutation<TResponse> {
    assertBankImportUploadPath(path);
    if (
      !(content instanceof Uint8Array) ||
      content.byteLength < 1 ||
      content.byteLength > MAX_BANK_IMPORT_BYTES
    ) {
      throw new ProtocolError();
    }
    const copied = new Uint8Array(content.byteLength);
    copied.set(content);
    const body = new Blob([copied], { type: contentType });
    const idempotencyKey = createIdempotencyKey(this.#randomSource);
    return {
      execute: (signal?: AbortSignal) =>
        this.#executeRawMutation<TResponse>(path, body, contentType, idempotencyKey, signal),
    };
  }

  public async logout(signal?: AbortSignal): Promise<void> {
    const path = "/api/v1/auth/logout";
    const csrfToken = readExactCsrfCookie(this.#readCookies());
    let response: Response;
    try {
      response = await this.#fetch(path, {
        ...BASE_REQUEST,
        method: "POST",
        headers: {
          Accept: "application/json",
          "X-CSRF-Token": csrfToken,
        },
        ...(signal === undefined ? {} : { signal }),
      });
    } catch {
      throw new NetworkError();
    }
    if (response.redirected || response.type === "opaqueredirect") {
      throw new ProtocolError();
    }
    if (response.status === 204) {
      return;
    }
    await parseJsonResponse<never>(response);
  }

  async #fetchGet(path: string, signal?: AbortSignal): Promise<Response> {
    return this.#fetch(path, {
      ...BASE_REQUEST,
      method: "GET",
      headers: { Accept: "application/json" },
      ...(signal === undefined ? {} : { signal }),
    });
  }

  async #executeMutation<TResponse>(
    path: string,
    method: MutationMethod,
    serializedBody: string,
    idempotencyKey: string,
    signal?: AbortSignal,
  ): Promise<TResponse> {
    const originalCsrf = readExactCsrfCookie(this.#readCookies());
    try {
      return await this.#sendMutation<TResponse>(
        path,
        method,
        serializedBody,
        idempotencyKey,
        originalCsrf,
        signal,
      );
    } catch (error) {
      if (!(error instanceof HttpApiError) || error.code !== "csrf_failed") {
        throw error;
      }
      let refreshedCsrf: string;
      try {
        refreshedCsrf = readExactCsrfCookie(this.#readCookies());
      } catch {
        this.#requireReopen();
      }
      if (refreshedCsrf === originalCsrf) {
        this.#requireReopen();
      }
      try {
        return await this.#sendMutation<TResponse>(
          path,
          method,
          serializedBody,
          idempotencyKey,
          refreshedCsrf,
          signal,
        );
      } catch (retryError) {
        if (retryError instanceof HttpApiError && retryError.code === "csrf_failed") {
          this.#requireReopen();
        }
        throw retryError;
      }
    }
  }

  async #executeRawMutation<TResponse>(
    path: string,
    body: Blob,
    contentType: RawMutationContentType,
    idempotencyKey: string,
    signal?: AbortSignal,
  ): Promise<TResponse> {
    const originalCsrf = readExactCsrfCookie(this.#readCookies());
    try {
      return await this.#sendRawMutation<TResponse>(
        path,
        body,
        contentType,
        idempotencyKey,
        originalCsrf,
        signal,
      );
    } catch (error) {
      if (!(error instanceof HttpApiError) || error.code !== "csrf_failed") {
        throw error;
      }
      let refreshedCsrf: string;
      try {
        refreshedCsrf = readExactCsrfCookie(this.#readCookies());
      } catch {
        this.#requireReopen();
      }
      if (refreshedCsrf === originalCsrf) {
        this.#requireReopen();
      }
      try {
        return await this.#sendRawMutation<TResponse>(
          path,
          body,
          contentType,
          idempotencyKey,
          refreshedCsrf,
          signal,
        );
      } catch (retryError) {
        if (retryError instanceof HttpApiError && retryError.code === "csrf_failed") {
          this.#requireReopen();
        }
        throw retryError;
      }
    }
  }

  #requireReopen(): never {
    this.#onProtectedUnauthorized();
    throw new ReopenRequiredError();
  }

  async #sendMutation<TResponse>(
    path: string,
    method: MutationMethod,
    serializedBody: string,
    idempotencyKey: string,
    csrfToken: string,
    signal?: AbortSignal,
  ): Promise<TResponse> {
    let response: Response;
    try {
      response = await this.#fetch(path, {
        ...BASE_REQUEST,
        method,
        headers: {
          ...JSON_HEADERS,
          "Idempotency-Key": idempotencyKey,
          "X-CSRF-Token": csrfToken,
        },
        body: serializedBody,
        ...(signal === undefined ? {} : { signal }),
      });
    } catch {
      throw new MutationResultUnknownError();
    }
    if (response.status >= 500) {
      await response.body?.cancel().catch(() => undefined);
      throw new MutationResultUnknownError();
    }
    if (response.status === 401) {
      this.#onProtectedUnauthorized();
    }
    if (response.status === 204) {
      return undefined as TResponse;
    }
    return parseJsonResponse<TResponse>(response);
  }

  async #sendRawMutation<TResponse>(
    path: string,
    body: Blob,
    contentType: RawMutationContentType,
    idempotencyKey: string,
    csrfToken: string,
    signal?: AbortSignal,
  ): Promise<TResponse> {
    let response: Response;
    try {
      response = await this.#fetch(path, {
        ...BASE_REQUEST,
        method: "POST",
        headers: {
          Accept: "application/json",
          "Content-Type": contentType,
          "Idempotency-Key": idempotencyKey,
          "X-CSRF-Token": csrfToken,
        },
        body,
        ...(signal === undefined ? {} : { signal }),
      });
    } catch {
      throw new MutationResultUnknownError();
    }
    if (response.status >= 500) {
      await response.body?.cancel().catch(() => undefined);
      throw new MutationResultUnknownError();
    }
    if (response.status === 401) {
      this.#onProtectedUnauthorized();
    }
    return parseJsonResponse<TResponse>(response);
  }
}
