export type TelegramThemeParams = Readonly<Record<string, unknown>>;

export interface TelegramSafeAreaInset {
  readonly top: number;
  readonly bottom: number;
  readonly left: number;
  readonly right: number;
}

export interface TelegramViewportChangedEvent {
  readonly isStateStable?: unknown;
}

export type TelegramWebAppEventName =
  | "activated"
  | "backButtonClicked"
  | "contentSafeAreaChanged"
  | "deactivated"
  | "safeAreaChanged"
  | "themeChanged"
  | "viewportChanged";

export type TelegramWebAppEventHandler = (...args: unknown[]) => void;

export interface TelegramBackButton {
  show(): void;
  hide(): void;
  onClick(callback: () => void): void;
  offClick(callback: () => void): void;
}

/** Minimal native SDK surface. Intentionally excludes initDataUnsafe. */
export interface TelegramWebApp {
  readonly initData: unknown;
  readonly version: unknown;
  readonly themeParams: TelegramThemeParams;
  readonly viewportStableHeight: unknown;
  readonly safeAreaInset?: TelegramSafeAreaInset;
  readonly contentSafeAreaInset?: TelegramSafeAreaInset;
  readonly BackButton: TelegramBackButton;
  ready(): void;
  expand(): void;
  close(): void;
  setHeaderColor(color: string): void;
  setBackgroundColor(color: string): void;
  setBottomBarColor?(color: string): void;
  onEvent(eventType: TelegramWebAppEventName, callback: TelegramWebAppEventHandler): void;
  offEvent(eventType: TelegramWebAppEventName, callback: TelegramWebAppEventHandler): void;
}

export interface TelegramWindow {
  readonly Telegram?: {
    readonly WebApp?: TelegramWebApp;
  };
}

export interface CssStyleTarget {
  readonly style: {
    setProperty(name: string, value: string): void;
  };
}

export interface TelegramMiniAppPort {
  initialize(): void;
  readRawInitDataForAuthentication(): string;
  subscribeTheme(listener: () => void): () => void;
  subscribeViewport(listener: () => void): () => void;
  setBackButton(visible: boolean, listener?: () => void): void;
  close(): void;
  dispose(): void;
}
