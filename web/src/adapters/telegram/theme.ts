import type { CssStyleTarget, TelegramWebApp } from "./telegram-web-app.types";

const THEME_PROPERTIES = {
  accent_text_color: "--tg-theme-accent-text-color",
  bg_color: "--tg-theme-bg-color",
  bottom_bar_bg_color: "--tg-theme-bottom-bar-bg-color",
  button_color: "--tg-theme-button-color",
  button_text_color: "--tg-theme-button-text-color",
  destructive_text_color: "--tg-theme-destructive-text-color",
  header_bg_color: "--tg-theme-header-bg-color",
  hint_color: "--tg-theme-hint-color",
  link_color: "--tg-theme-link-color",
  secondary_bg_color: "--tg-theme-secondary-bg-color",
  section_bg_color: "--tg-theme-section-bg-color",
  section_header_text_color: "--tg-theme-section-header-text-color",
  subtitle_text_color: "--tg-theme-subtitle-text-color",
  text_color: "--tg-theme-text-color",
} as const;

const HEX_COLOR = /^#[0-9a-fA-F]{6}$/;

const SEMANTIC_COLORS = {
  dark: {
    danger: "#df7771",
    expense: "#d38a70",
    focus: "#f3d896",
    income: "#6dba9f",
  },
  light: {
    danger: "#b54843",
    expense: "#a8543d",
    focus: "#6f531c",
    income: "#347961",
  },
} as const;

function backgroundColorScheme(value: string): "dark" | "light" {
  const red = Number.parseInt(value.slice(1, 3), 16);
  const green = Number.parseInt(value.slice(3, 5), 16);
  const blue = Number.parseInt(value.slice(5, 7), 16);
  return (red * 299 + green * 587 + blue * 114) / 1000 >= 150 ? "light" : "dark";
}

function projectSemanticTheme(background: string, target: CssStyleTarget): void {
  const scheme = backgroundColorScheme(background);
  const colors = SEMANTIC_COLORS[scheme];
  target.style.setProperty("--nm-color-scheme", scheme);
  target.style.setProperty("--nm-danger", colors.danger);
  target.style.setProperty("--nm-expense", colors.expense);
  target.style.setProperty("--nm-focus", colors.focus);
  target.style.setProperty("--nm-income", colors.income);
}

export function isTelegramVersionAtLeast(version: unknown, required: string): boolean {
  if (typeof version !== "string" || !/^\d+(?:\.\d+)*$/.test(version)) {
    return false;
  }

  const currentParts = version.split(".").map(Number);
  const requiredParts = required.split(".").map(Number);
  const length = Math.max(currentParts.length, requiredParts.length);
  for (let index = 0; index < length; index += 1) {
    const current = currentParts[index] ?? 0;
    const expected = requiredParts[index] ?? 0;
    if (current !== expected) {
      return current > expected;
    }
  }
  return true;
}

export function projectTelegramTheme(webApp: TelegramWebApp, target: CssStyleTarget): boolean {
  let projected = false;
  for (const [themeKey, cssProperty] of Object.entries(THEME_PROPERTIES)) {
    const value = webApp.themeParams[themeKey];
    if (typeof value === "string" && HEX_COLOR.test(value)) {
      target.style.setProperty(cssProperty, value.toLowerCase());
      projected = true;
    }
  }
  const background = webApp.themeParams.bg_color;
  if (typeof background === "string" && HEX_COLOR.test(background)) {
    projectSemanticTheme(background, target);
  }
  return projected;
}

export function synchronizeTelegramChrome(webApp: TelegramWebApp): void {
  webApp.setHeaderColor("bg_color");
  webApp.setBackgroundColor("bg_color");
  if (isTelegramVersionAtLeast(webApp.version, "7.10")) {
    webApp.setBottomBarColor?.("bottom_bar_bg_color");
  }
}
