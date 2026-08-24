// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, renderHook } from "@testing-library/react";
import type { PropsWithChildren } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

const mutationRun = vi.hoisted(() => vi.fn());

vi.mock("../../shared/mutations/prepared-mutation", () => ({
  usePreparedMutation: () => ({
    error: null,
    isPending: false,
    outcomeUnknown: false,
    retryUnknown: vi.fn(),
    run: mutationRun,
  }),
}));

import { useQuickDraftMutation } from "./use-quick-draft-mutation";

afterEach(() => {
  cleanup();
  mutationRun.mockReset();
});

describe("useQuickDraftMutation", () => {
  it("uses the canonical prepared mutation boundary for quick ingress", () => {
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const wrapper = ({ children }: PropsWithChildren) => (
      <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
    );
    const { result } = renderHook(() => useQuickDraftMutation(), { wrapper });

    act(() => result.current.submit("1 200,50"));

    expect(mutationRun).toHaveBeenCalledWith({
      path: "/api/v1/drafts/quick",
      body: { text: "1 200,50" },
      context: "quick",
    });
  });
});
