const CANONICAL_MINOR_UNITS = /^-?\d+$/u;
// Numismat's domain parser and CSV contract use two minor digits for every allowed currency code.
const MINOR_DIGITS = 2;

function normalizeLocale(locale: string): string {
  try {
    return Intl.getCanonicalLocales(locale)[0] ?? "ru-RU";
  } catch {
    return "ru-RU";
  }
}

function fallbackMoney(
  minorUnits: bigint,
  currency: string,
  fractionDigits: number,
  locale: string,
): string {
  const negative = minorUnits < 0n;
  const absolute = negative ? -minorUnits : minorUnits;
  const scale = 10n ** BigInt(fractionDigits);
  const whole = absolute / scale;
  const fraction = (absolute % scale).toString().padStart(fractionDigits, "0");
  const grouped = new Intl.NumberFormat(locale, { maximumFractionDigits: 0 }).format(whole);
  return `${negative ? "−" : ""}${grouped}${fractionDigits > 0 ? `.${fraction}` : ""} ${currency}`;
}

/** Formats canonical integer minor units without ever converting the amount to Number. */
export function formatMoney(minor: string, currency: string, locale = "ru-RU"): string {
  if (!CANONICAL_MINOR_UNITS.test(minor)) {
    return `— ${currency}`;
  }

  const canonicalLocale = normalizeLocale(locale);
  const minorUnits = BigInt(minor);
  const fractionDigits = MINOR_DIGITS;
  const negative = minorUnits < 0n;
  const absolute = negative ? -minorUnits : minorUnits;
  const scale = 10n ** BigInt(fractionDigits);
  const whole = absolute / scale;
  const fraction = (absolute % scale).toString().padStart(fractionDigits, "0");

  try {
    const currencyParts = new Intl.NumberFormat(canonicalLocale, {
      style: "currency",
      currency,
      currencyDisplay: "narrowSymbol",
      minimumFractionDigits: fractionDigits,
      maximumFractionDigits: fractionDigits,
    }).formatToParts(0);
    const groupedWhole = new Intl.NumberFormat(canonicalLocale, {
      useGrouping: true,
      maximumFractionDigits: 0,
    }).format(whole);
    const rendered = currencyParts
      .filter((part) => part.type !== "minusSign")
      .map((part) => {
        if (part.type === "integer") {
          return groupedWhole;
        }
        if (part.type === "fraction") {
          return fraction;
        }
        return part.value;
      })
      .join("");
    return negative ? `−${rendered}` : rendered;
  } catch {
    return fallbackMoney(minorUnits, currency, fractionDigits, canonicalLocale);
  }
}

export function formatTransactionMoney(
  minor: string,
  currency: string,
  type: "expense" | "income",
  locale = "ru-RU",
): string {
  const unsigned = minor.startsWith("-") ? minor.slice(1) : minor;
  return `${type === "income" ? "+" : "−"}${formatMoney(unsigned, currency, locale)}`;
}

export function moneyMagnitude(minor: string): bigint {
  if (!CANONICAL_MINOR_UNITS.test(minor)) {
    return 0n;
  }
  const value = BigInt(minor);
  return value < 0n ? -value : value;
}
