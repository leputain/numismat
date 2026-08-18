import { describe, expect, it, vi } from "vitest";

import type {
  CssStyleTarget,
  TelegramWebApp,
  TelegramWebAppEventHandler,
  TelegramWebAppEventName,
} from "./telegram-web-app.types";
import { NativeTelegramMiniApp } from "./telegram-web-app";

function fixture() {
  const handlers = new Map<TelegramWebAppEventName, Set<TelegramWebAppEventHandler>>();
  const css = new Map<string, string>();
  const backClick = new Set<() => void>();
  const webApp: TelegramWebApp = {
    initData: "query_id=opaque%2Bwire&hash=not-parsed",
    version: "8.0",
    themeParams: { bg_color: "#AABBCC", text_color: "javascript:bad", unknown: "#ffffff" },
    viewportStableHeight: 701,
    safeAreaInset: { top: 1, right: 2, bottom: 3, left: 4 },
    contentSafeAreaInset: { top: 5, right: 6, bottom: 7, left: 8 },
    BackButton: {
      show: vi.fn(),
      hide: vi.fn(),
      onClick: (callback) => backClick.add(callback),
      offClick: (callback) => backClick.delete(callback),
    },
    ready: vi.fn(),
    expand: vi.fn(),
    close: vi.fn(),
    setHeaderColor: vi.fn(),
    setBackgroundColor: vi.fn(),
    setBottomBarColor: vi.fn(),
    onEvent(event, callback) {
      const callbacks = handlers.get(event) ?? new Set();
      callbacks.add(callback);
      handlers.set(event, callbacks);
    },
    offEvent(event, callback) {
      handlers.get(event)?.delete(callback);
    },
  };
  const target: CssStyleTarget = {
    style: { setProperty: (name, value) => css.set(name, value) },
  };
  return { webApp, target, handlers, css, backClick };
}

describe("NativeTelegramMiniApp", () => {
  it("projects only stable allowlisted state and cleans native callbacks", () => {
    const { webApp, target, handlers, css, backClick } = fixture();
    const adapter = new NativeTelegramMiniApp(webApp, target);

    adapter.initialize();
    adapter.initialize();
    expect(webApp.ready).toHaveBeenCalledTimes(1);
    expect(webApp.expand).toHaveBeenCalledTimes(1);
    expect(css.get("--tg-theme-bg-color")).toBe("#aabbcc");
    expect(css.has("--tg-theme-text-color")).toBe(false);
    expect(adapter.readRawInitDataForAuthentication()).toBe(
      "query_id=opaque%2Bwire&hash=not-parsed",
    );

    const viewportListener = vi.fn();
    const unsubscribeTheme = adapter.subscribeTheme(() => undefined);
    const unsubscribeViewport = adapter.subscribeViewport(viewportListener);
    handlers.get("viewportChanged")?.forEach((handler) => handler({ isStateStable: false }));
    expect(viewportListener).not.toHaveBeenCalled();
    handlers.get("viewportChanged")?.forEach((handler) => handler({ isStateStable: true }));
    expect(viewportListener).toHaveBeenCalledTimes(1);

    const back = vi.fn();
    adapter.setBackButton(true, back);
    expect(backClick.has(back)).toBe(true);
    adapter.setBackButton(false);
    expect(backClick.has(back)).toBe(false);
    unsubscribeViewport();
    unsubscribeTheme();
    expect([...handlers.values()].every((callbacks) => callbacks.size === 0)).toBe(true);
  });
});
