import { describe, expect, it } from "vitest";

import { formatExclusivePeriod, formatPeriod } from "../format/date-time";
import { formatMinorAmount, formatMoney, formatTransactionMoney } from "./money";

describe("financial formatting smoke", () => {
  it("keeps exact money and owner-timezone period boundaries", () => {
    expect(formatMoney("900719925474099312345", "USD", "en-US")).toBe(
      "$9,007,199,254,740,993,123.45",
    );
    expect(formatTransactionMoney("12345", "USD", "expense", "en-US")).toBe("−$123.45");
    expect(formatMinorAmount("900719925474099312345", "ru-RU")).toBe(
      "9 007 199 254 740 993 123,45",
    );
    expect(formatMinorAmount("not-money", "ru-RU")).toBe("—");
    expect(formatPeriod("2026-01-01T22:30:00Z", "en-US", "Europe/Moscow")).toBe("Jan 2");
    expect(
      formatExclusivePeriod(
        "2026-02-28T21:00:00Z",
        "2026-03-31T21:00:00Z",
        "en-US",
        "Europe/Moscow",
      ),
    ).toBe("Mar 1 — Mar 31");
  });
});
