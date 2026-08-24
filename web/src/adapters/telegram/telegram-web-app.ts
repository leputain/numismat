import { emitClientEvent } from "../../shared/logging/client-events";
import { projectTelegramTheme, synchronizeTelegramChrome } from "./theme";
import type {
  CssStyleTarget,
  TelegramMiniAppPort,
  TelegramWebApp,
  TelegramWebAppEventHandler,
  TelegramWebAppEventName,
  TelegramWindow,
} from "./telegram-web-app.types";
import { isStableViewportEvent, projectTelegramViewport } from "./viewport";
import { isTelegramVersionAtLeast } from "./theme";

const MAX_INIT_DATA_LENGTH = 8192;

export class TelegramSdkUnavailableError extends Error {
  public constructor() {
    super("Telegram Mini App is unavailable");
    this.name = "TelegramSdkUnavailableError";
  }
}

function noOperation(): void {}

export class NativeTelegramMiniApp implements TelegramMiniAppPort {
  readonly #webApp: TelegramWebApp;
  readonly #styleTarget: CssStyleTarget;
  readonly #subscriptions = new Set<() => void>();
  #initialized = false;
  #backListener: (() => void) | undefined;

  public constructor(webApp: TelegramWebApp, styleTarget: CssStyleTarget) {
    this.#webApp = webApp;
    this.#styleTarget = styleTarget;
  }

  public initialize(): void {
    if (this.#initialized) {
      return;
    }
    this.#initialized = true;
    this.#safeSdkCall(() => projectTelegramTheme(this.#webApp, this.#styleTarget));
    this.#safeSdkCall(() => projectTelegramViewport(this.#webApp, this.#styleTarget));
    this.#safeSdkCall(() => synchronizeTelegramChrome(this.#webApp));
    this.#safeSdkCall(() => this.#webApp.expand());
    this.#safeSdkCall(() => this.#webApp.ready());
  }

  public readRawInitDataForAuthentication(): string {
    const value = this.#webApp.initData;
    if (typeof value !== "string" || value.length < 1 || value.length > MAX_INIT_DATA_LENGTH) {
      throw new TelegramSdkUnavailableError();
    }
    return value;
  }

  public subscribeTheme(listener: () => void): () => void {
    const handler: TelegramWebAppEventHandler = () => {
      this.#safeSdkCall(() => {
        const projected = projectTelegramTheme(this.#webApp, this.#styleTarget);
        synchronizeTelegramChrome(this.#webApp);
        if (projected) {
          listener();
        }
      });
    };
    return this.#subscribe("themeChanged", handler);
  }

  public subscribeViewport(listener: () => void): () => void {
    const cleanups: Array<() => void> = [];
    const viewportHandler: TelegramWebAppEventHandler = (event) => {
      if (!isStableViewportEvent(event)) {
        return;
      }
      this.#safeSdkCall(() => {
        if (projectTelegramViewport(this.#webApp, this.#styleTarget)) {
          listener();
        }
      });
    };
    cleanups.push(this.#subscribe("viewportChanged", viewportHandler));

    if (this.#isVersionAtLeast("8.0")) {
      const insetHandler: TelegramWebAppEventHandler = () => {
        this.#safeSdkCall(() => {
          if (projectTelegramViewport(this.#webApp, this.#styleTarget)) {
            listener();
          }
        });
      };
      cleanups.push(this.#subscribe("safeAreaChanged", insetHandler));
      cleanups.push(this.#subscribe("contentSafeAreaChanged", insetHandler));
    }

    let active = true;
    return () => {
      if (!active) {
        return;
      }
      active = false;
      for (const cleanup of cleanups) {
        cleanup();
      }
    };
  }

  public setBackButton(visible: boolean, listener?: () => void): void {
    this.#clearBackListener();
    if (!this.#isVersionAtLeast("6.1")) {
      return;
    }
    this.#safeSdkCall(() => {
      if (visible) {
        if (listener !== undefined) {
          this.#backListener = listener;
          this.#webApp.BackButton.onClick(listener);
        }
        this.#webApp.BackButton.show();
      } else {
        this.#webApp.BackButton.hide();
      }
    });
  }

  #clearBackListener(): void {
    if (this.#backListener === undefined) {
      return;
    }
    const listener = this.#backListener;
    this.#backListener = undefined;
    this.#safeSdkCall(() => this.#webApp.BackButton.offClick(listener));
  }

  public close(): void {
    this.#safeSdkCall(() => this.#webApp.close());
  }

  public dispose(): void {
    for (const cleanup of [...this.#subscriptions]) {
      cleanup();
    }
    this.setBackButton(false);
  }

  #subscribe(eventName: TelegramWebAppEventName, handler: TelegramWebAppEventHandler): () => void {
    try {
      this.#webApp.onEvent(eventName, handler);
    } catch {
      emitClientEvent("telegram_sdk_lifecycle_failed");
      return noOperation;
    }

    let active = true;
    const cleanup = () => {
      if (!active) {
        return;
      }
      active = false;
      this.#subscriptions.delete(cleanup);
      this.#safeSdkCall(() => this.#webApp.offEvent(eventName, handler));
    };
    this.#subscriptions.add(cleanup);
    return cleanup;
  }

  #safeSdkCall(action: () => void): void {
    try {
      action();
    } catch {
      emitClientEvent("telegram_sdk_lifecycle_failed");
    }
  }

  #isVersionAtLeast(required: string): boolean {
    try {
      return isTelegramVersionAtLeast(this.#webApp.version, required);
    } catch {
      emitClientEvent("telegram_sdk_lifecycle_failed");
      return false;
    }
  }
}

export function createTelegramMiniApp(
  host: TelegramWindow | undefined =
    typeof window === "undefined" ? undefined : (window as unknown as TelegramWindow),
  styleTarget: CssStyleTarget | undefined =
    typeof document === "undefined" ? undefined : document.documentElement,
): TelegramMiniAppPort {
  let webApp: TelegramWebApp | undefined;
  try {
    webApp = host?.Telegram?.WebApp;
  } catch {
    emitClientEvent("telegram_sdk_lifecycle_failed");
    return unavailableTelegramMiniApp();
  }
  try {
    if (!isUsableWebApp(webApp) || styleTarget === undefined) {
      return unavailableTelegramMiniApp();
    }
  } catch {
    emitClientEvent("telegram_sdk_lifecycle_failed");
    return unavailableTelegramMiniApp();
  }
  return new NativeTelegramMiniApp(webApp, styleTarget);
}

function unavailableTelegramMiniApp(): TelegramMiniAppPort {
  return {
    initialize(): never {
      throw new TelegramSdkUnavailableError();
    },
    readRawInitDataForAuthentication(): never {
      throw new TelegramSdkUnavailableError();
    },
    subscribeTheme: () => noOperation,
    subscribeViewport: () => noOperation,
    setBackButton: noOperation,
    close: noOperation,
    dispose: noOperation,
  };
}

function isUsableWebApp(value: TelegramWebApp | undefined): value is TelegramWebApp {
  return (
    value !== undefined &&
    typeof value.ready === "function" &&
    typeof value.expand === "function" &&
    typeof value.close === "function" &&
    typeof value.onEvent === "function" &&
    typeof value.offEvent === "function" &&
    typeof value.setHeaderColor === "function" &&
    typeof value.setBackgroundColor === "function" &&
    typeof value.BackButton === "object" &&
    value.BackButton !== null &&
    typeof value.BackButton.show === "function" &&
    typeof value.BackButton.hide === "function" &&
    typeof value.BackButton.onClick === "function" &&
    typeof value.BackButton.offClick === "function" &&
    typeof value.themeParams === "object" &&
    value.themeParams !== null
  );
}
