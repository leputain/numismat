import {
  createContext,
  useContext,
  useEffect,
  useMemo,
  useState,
} from "react";
import type { PropsWithChildren } from "react";

import type { TelegramMiniAppPort } from "../../adapters/telegram/telegram-web-app.types";
import type { AuthState } from "./auth-coordinator";
import { AuthCoordinator } from "./auth-coordinator";

interface AuthContextValue {
  readonly state: AuthState;
  readonly telegram: TelegramMiniAppPort;
  retrySessionCheck(): Promise<void>;
  logout(): Promise<void>;
  closeMiniApp(): void;
}

const AuthContext = createContext<AuthContextValue | undefined>(undefined);

interface AuthProviderProps extends PropsWithChildren {
  readonly coordinator: AuthCoordinator;
  readonly telegram: TelegramMiniAppPort;
}

export function AuthProvider({ coordinator, telegram, children }: AuthProviderProps) {
  const [state, setState] = useState<AuthState>(coordinator.state);

  useEffect(() => {
    const unsubscribeState = coordinator.subscribe(setState);
    const unsubscribeTheme = telegram.subscribeTheme(() => undefined);
    const unsubscribeViewport = telegram.subscribeViewport(() => undefined);
    void coordinator.start();

    const handlePageShow = (event: PageTransitionEvent) => {
      if (event.persisted) {
        void coordinator.recheckAuthenticatedSession();
      }
    };
    window.addEventListener("pageshow", handlePageShow);
    return () => {
      window.removeEventListener("pageshow", handlePageShow);
      unsubscribeViewport();
      unsubscribeTheme();
      unsubscribeState();
    };
  }, [coordinator, telegram]);

  const value = useMemo<AuthContextValue>(
    () => ({
      state,
      telegram,
      async retrySessionCheck(): Promise<void> {
        await coordinator.retrySessionCheck();
      },
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
