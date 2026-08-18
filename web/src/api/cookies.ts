import { ProtocolError } from "./errors";

export const CSRF_COOKIE_NAME = "__Host-numismat_csrf";

const CANONICAL_OPAQUE_TOKEN = /^[A-Za-z0-9_-]{42}[AEIMQUYcgkosw048]$/;

export function isCanonicalOpaqueToken(value: unknown): value is string {
  return typeof value === "string" && CANONICAL_OPAQUE_TOKEN.test(value);
}

/** Reads exactly one cookie without URL-decoding attacker-controlled bytes. */
export function readExactCsrfCookie(cookieHeader: string): string {
  const matches: string[] = [];
  for (const segment of cookieHeader.split(";")) {
    const trimmed = segment.trim();
    const separator = trimmed.indexOf("=");
    if (separator < 0 || trimmed.slice(0, separator) !== CSRF_COOKIE_NAME) {
      continue;
    }
    matches.push(trimmed.slice(separator + 1));
  }

  const value = matches[0];
  if (matches.length !== 1 || value === undefined || !isCanonicalOpaqueToken(value)) {
    throw new ProtocolError();
  }
  return value;
}
