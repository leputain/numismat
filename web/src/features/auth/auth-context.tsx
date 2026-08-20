import {
  createContext,
  useContext,
  useEffect,
  useMemo,
  useState,
} from "react";
import type { PropsWithChildren } from "react";
import { flushSync } from "react-dom";

import type { TelegramMiniAppPort } from "../../adapters/telegram/telegram-web-app.types";
import type { AuthState } from "./auth-coordinator";
import { AuthCoordinator } from "./auth-coordinator";

interface AuthContextValue {
  readonly state: AuthState;
  readonly telegram: TelegramMiniAppPort;
  logout(): Promise<void>;
  closeMiniApp(): void;
}

const AuthContext = createContext<AuthContextValue | undefined>(undefined);

interface AuthProviderProps extends PropsWithChildren {
  readonly coordinator: AuthCoordinator;
  readonly telegram: TelegramMiniAppPort;
}

type AuthPageLifecycleCoordinator = Pick<
  AuthCoordinator,
  "recheckAuthenticatedSession" | "suspendProtectedSession"
>;

interface VisibilityLifecycleTarget {
  readonly visibilityState: DocumentVisibilityState;
  addEventListener(type: "visibilitychange", listener: EventListener): void;
  removeEventListener(type: "visibilitychange", listener: EventListener): void;
}

interface PageLifecycleTarget {
  addEventListener(type: "pagehide" | "pageshow", listener: EventListener): void;
  removeEventListener(type: "pagehide" | "pageshow", listener: EventListener): void;
}

export function installAuthPageLifecycle(
  coordinator: AuthPageLifecycleCoordinator,
  visibilityTarget: VisibilityLifecycleTarget = document,
  pageTarget: PageLifecycleTarget = window,
): () => void {
  let suspended = false;

  const suspend = () => {
    if (suspended) {
      return;
    }
    suspended = true;
    flushSync(() => coordinator.suspendProtectedSession());
  };
  const restore = () => {
    if (!suspended) {
      return;
    }
    suspended = false;
    void coordinator.recheckAuthenticatedSession();
  };
  const handleVisibilityChange: EventListener = () => {
    if (visibilityTarget.visibilityState === "hidden") {
      suspend();
    } else if (visibilityTarget.visibilityState === "visible") {
      restore();
    }
  };
  const handlePageHide: EventListener = () => suspend();
  const handlePageShow: EventListener = (event) => {
    if ((event as PageTransitionEvent).persisted) {
      restore();
    }
  };

  visibilityTarget.addEventListener("visibilitychange", handleVisibilityChange);
  pageTarget.addEventListener("pagehide", handlePageHide);
  pageTarget.addEventListener("pageshow", handlePageShow);
  if (visibilityTarget.visibilityState === "hidden") {
    suspend();
  }
  return () => {
    visibilityTarget.removeEventListener("visibilitychange", handleVisibilityChange);
    pageTarget.removeEventListener("pagehide", handlePageHide);
    pageTarget.removeEventListener("pageshow", handlePageShow);
  };
}

export function AuthProvider({ coordinator, telegram, children }: AuthProviderProps) {
  const [state, setState] = useState<AuthState>(coordinator.state);

  useEffect(() => {
    const unsubscribeState = coordinator.subscribe(setState);
    const unsubscribeTheme = telegram.subscribeTheme(() => undefined);
    const unsubscribeViewport = telegram.subscribeViewport(() => undefined);
    void coordinator.start();
    const uninstallAuthPageLifecycle = installAuthPageLifecycle(coordinator);
    return () => {
      uninstallAuthPageLifecycle();
      unsubscribeViewport();
      unsubscribeTheme();
      unsubscribeState();
    };
  }, [coordinator, telegram]);

  const value = useMemo<AuthContextValue>(
    () => ({
      state,
      telegram,
      async logout(): Promise<void> {
        await coordinator.logout();
      },
      closeMiniApp(): void {
        coordinator.closeMiniApp();
      },
    }),
    [coordinator, state, telegram],
  );

  return <AuthContext value={value}>{children}</AuthContext>;
}

export function useAuth(): AuthContextValue {
  const context = useContext(AuthContext);
  if (context === undefined) {
    throw new Error("Auth provider is unavailable");
  }
  return context;
}
