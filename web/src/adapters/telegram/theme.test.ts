import { describe, expect, it } from "vitest";

import type { CssStyleTarget, TelegramWebApp } from "./telegram-web-app.types";
import { projectTelegramTheme } from "./theme";

function project(background: string): Map<string, string> {
  const values = new Map<string, string>();
  const target: CssStyleTarget = {
    style: { setProperty: (name, value) => values.set(name, value) },
  };
  projectTelegramTheme(
    { themeParams: { bg_color: background } } as unknown as TelegramWebApp,
    target,
  );
  return values;
}

describe("Telegram theme projection", () => {
  it("derives semantic contrast from the actual Telegram background", () => {
    expect(project("#f8f7f2").get("--nm-color-scheme")).toBe("light");
    expect(project("#f8f7f2").get("--nm-income")).toBe("#347961");
    expect(project("#071012").get("--nm-color-scheme")).toBe("dark");
    expect(project("#071012").get("--nm-income")).toBe("#6dba9f");
  });
});
