import { ProtocolError } from "./errors";

export type RandomSource = Pick<Crypto, "getRandomValues">;

export function createIdempotencyKey(randomSource: RandomSource = crypto): string {
  const bytes = new Uint8Array(32);
  randomSource.getRandomValues(bytes);
  let binary = "";
  for (const byte of bytes) {
    binary += String.fromCharCode(byte);
  }
  const key = btoa(binary).replaceAll("+", "-").replaceAll("/", "_").replace(/=+$/u, "");
  if (key.length !== 43) {
    throw new ProtocolError();
  }
  return key;
}
