const MAJOR_AMOUNT = /^(?:0|[1-9]\d*)(?:[.,](\d{1,2}))?$/u;
const MINOR_AMOUNT = /^(?:0|[1-9]\d*)$/u;
const MAX_MINOR = 9_223_372_036_854_775_807n;

export function majorToMinor(value: string): string | undefined {
  const normalized = value.trim().replaceAll(" ", "").replaceAll("\u00a0", "");
  const match = MAJOR_AMOUNT.exec(normalized);
  if (match === null) {
    return undefined;
  }
  const [wholeText, fractionText = ""] = normalized.replace(",", ".").split(".");
  if (wholeText === undefined) {
    return undefined;
  }
  const minor = BigInt(wholeText) * 100n + BigInt(fractionText.padEnd(2, "0") || "0");
  return minor > 0n && minor <= MAX_MINOR ? minor.toString() : undefined;
}

export function minorToMajor(value: string): string {
  if (!MINOR_AMOUNT.test(value)) {
    return "";
  }
  const minor = BigInt(value);
  const whole = minor / 100n;
  const fraction = (minor % 100n).toString().padStart(2, "0");
  return fraction === "00" ? whole.toString() : `${whole.toString()},${fraction}`;
}

export function validBudgetPeriod(startsOn: string, endsOn: string): boolean {
  const start = Date.parse(`${startsOn}T00:00:00Z`);
  const end = Date.parse(`${endsOn}T00:00:00Z`);
  if (!Number.isFinite(start) || !Number.isFinite(end) || end < start) {
    return false;
  }
  const days = Math.floor((end - start) / 86_400_000) + 1;
  return days >= 1 && days <= 366;
}
