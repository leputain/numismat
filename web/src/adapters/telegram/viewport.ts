import type {
  CssStyleTarget,
  TelegramSafeAreaInset,
  TelegramWebApp,
} from "./telegram-web-app.types";
import { isTelegramVersionAtLeast } from "./theme";

function isValidDimension(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value) && value >= 0;
}

function projectInset(
  prefix: "safe-area" | "content-safe-area",
  inset: TelegramSafeAreaInset | undefined,
  target: CssStyleTarget,
): boolean {
  if (inset === undefined) {
    return false;
  }

  let projected = false;
  for (const edge of ["top", "right", "bottom", "left"] as const) {
    const value = inset[edge];
    if (isValidDimension(value)) {
      target.style.setProperty(`--tg-${prefix}-${edge}`, `${value}px`);
      projected = true;
    }
  }
  return projected;
}

export function projectTelegramViewport(webApp: TelegramWebApp, target: CssStyleTarget): boolean {
  let projected = false;
  if (isValidDimension(webApp.viewportStableHeight) && webApp.viewportStableHeight > 0) {
    target.style.setProperty("--tg-viewport-stable-height", `${webApp.viewportStableHeight}px`);
    projected = true;
  }

  if (isTelegramVersionAtLeast(webApp.version, "8.0")) {
    projected = projectInset("safe-area", webApp.safeAreaInset, target) || projected;
    projected = projectInset("content-safe-area", webApp.contentSafeAreaInset, target) || projected;
  }
  return projected;
}

export function isStableViewportEvent(value: unknown): boolean {
  return (
    typeof value === "object" &&
    value !== null &&
    "isStateStable" in value &&
    value.isStateStable === true
  );
}
