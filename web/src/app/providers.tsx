import { QueryClientProvider } from "@tanstack/react-query";
import { useEffect } from "react";
import type { PropsWithChildren } from "react";
import { BrowserRouter } from "react-router";

import { createTelegramMiniApp } from "../adapters/telegram/telegram-web-app";
import { SameOriginApiClient } from "../api/client";
import { createAuthApi } from "../features/auth/auth-api";
import { AuthCoordinator } from "../features/auth/auth-coordinator";
import { AuthProvider } from "../features/auth/auth-context";
import { createAppQueryClient, installBrowserFocusTracking } from "./query-client";

export const appQueryClient = createAppQueryClient();
export const telegramMiniApp = createTelegramMiniApp();
const coordinatorReference: { current?: AuthCoordinator } = {};
export const apiClient = new SameOriginApiClient({
  onProtectedUnauthorized: () => coordinatorReference.current?.handleProtectedUnauthorized(),
});
export const appAuthCoordinator = new AuthCoordinator({
  api: createAuthApi(apiClient),
  telegram: telegramMiniApp,
  clearProtectedState: () => appQueryClient.clear(),
});
coordinatorReference.current = appAuthCoordinator;

function QueryFocusProvider({ children }: PropsWithChildren) {
  useEffect(() => installBrowserFocusTracking(), []);
  return children;
}

export function AppProviders({ children }: PropsWithChildren) {
  return (
    <QueryClientProvider client={appQueryClient}>
      <QueryFocusProvider>
        <AuthProvider coordinator={appAuthCoordinator} telegram={telegramMiniApp}>
          <BrowserRouter>{children}</BrowserRouter>
        </AuthProvider>
      </QueryFocusProvider>
    </QueryClientProvider>
  );
}
