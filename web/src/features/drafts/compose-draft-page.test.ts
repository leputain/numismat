import { describe, expect, it } from "vitest";

import { isComposeAmount, ownerToday } from "./compose-draft-page";

describe("manual compose input", () => {
  it.each(["1", "500", "500.5", "500.50", "500,5", "500,50"])(
    "accepts a positive decimal value %s",
    (value) => expect(isComposeAmount(value)).toBe(true),
  );

  it.each(["", "0", "0.00", "+500", "-500", "1 000", "1.000,50", "5e2", ".5"])(
    "rejects ambiguous or non-positive value %s",
    (value) => expect(isComposeAmount(value)).toBe(false),
  );

  it("uses the owner timezone for the default date", () => {
    const instant = new Date("2026-08-24T22:30:00.000Z");
    expect(ownerToday("Europe/Moscow", instant)).toBe("2026-08-25");
    expect(ownerToday("America/New_York", instant)).toBe("2026-08-24");
  });
});
