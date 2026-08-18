import { QueryClient, focusManager } from "@tanstack/react-query";

export function createAppQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        staleTime: 30_000,
        gcTime: 5 * 60_000,
        // SameOriginApiClient owns the single bounded GET retry.
        retry: false,
        refetchOnReconnect: true,
        refetchOnWindowFocus: true,
      },
      mutations: {
        retry: false,
        // Execute once and fail while offline; never pause/resume as an offline queue.
        networkMode: "always",
      },
    },
  });
}

export function installBrowserFocusTracking(): () => void {
  if (typeof window === "undefined" || typeof document === "undefined") {
    return () => undefined;
  }

  focusManager.setEventListener((handleFocus) => {
    const updateFocus = () => handleFocus(document.visibilityState !== "hidden");
    window.addEventListener("focus", updateFocus);
    document.addEventListener("visibilitychange", updateFocus);
    return () => {
      window.removeEventListener("focus", updateFocus);
      document.removeEventListener("visibilitychange", updateFocus);
    };
  });
  return () => {
    focusManager.setEventListener(() => () => undefined);
    focusManager.setFocused(undefined);
  };
}
