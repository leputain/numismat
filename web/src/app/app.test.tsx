import { renderToStaticMarkup } from "react-dom/server";
import { MemoryRouter } from "react-router";
import { describe, expect, it } from "vitest";

import { OfflineBanner } from "./shell/offline-banner";
import { PrimaryNavigation } from "./shell/primary-navigation";
import { AuthErrorState } from "./states/auth-error-state";
import { LoadingState } from "./states/loading-state";
import { RouteErrorState } from "./states/route-error-state";
import { isCanonicalTransactionId } from "./routes";

describe("protected route shell semantics", () => {
  it("renders accessible loading, offline, navigation and non-reflective route errors", () => {
    const loading = renderToStaticMarkup(<LoadingState />);
    const authError = renderToStaticMarkup(
      <AuthErrorState
        actionLabel="Закрыть"
        description="Безопасное описание"
        onAction={() => undefined}
        title="Нужен новый вход"
      />,
    );
    const shell = renderToStaticMarkup(
      <MemoryRouter initialEntries={["/transactions"]}>
        <OfflineBanner offline />
        <PrimaryNavigation />
        <RouteErrorState />
      </MemoryRouter>,
    );

    expect(loading).toContain('aria-busy="true"');
    expect(loading).toContain('role="status"');
    expect(authError).toContain('role="alert"');
    expect(shell).toContain("Нет сети");
    expect(shell).toContain('aria-label="Основная навигация"');
    expect(shell).toContain('aria-current="page"');
    expect(shell).toContain("Страница не найдена");
    expect(isCanonicalTransactionId("NOT-A-PRIVATE-ID")).toBe(false);
    expect(shell).not.toContain("NOT-A-PRIVATE-ID");
  });
});
