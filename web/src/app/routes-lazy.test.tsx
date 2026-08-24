// @vitest-environment jsdom

import { lazy } from "react";
import { act, cleanup, render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router";
import { afterEach, describe, expect, it } from "vitest";

import { RouteLoadingBoundary } from "./routes";

afterEach(cleanup);

describe("lazy route boundary", () => {
  it("keeps an accessible skeleton mounted until the named page chunk resolves", async () => {
    let resolvePage: ((module: { default: () => React.JSX.Element }) => void) | undefined;
    const DeferredPage = lazy(
      () =>
        new Promise<{ default: () => React.JSX.Element }>((resolve) => {
          resolvePage = resolve;
        }),
    );

    render(
      <MemoryRouter>
        <Routes>
          <Route element={<RouteLoadingBoundary />}>
            <Route element={<DeferredPage />} index />
          </Route>
        </Routes>
      </MemoryRouter>,
    );

    expect(screen.getByRole("status", { name: "Загрузка" })).not.toBeNull();
    expect(screen.getByText("Загрузка данных…")).not.toBeNull();

    await act(async () => {
      resolvePage?.({ default: () => <h1>Ленивая страница готова</h1> });
    });

    expect(await screen.findByRole("heading", { name: "Ленивая страница готова" })).not.toBeNull();
    expect(screen.queryByRole("status", { name: "Загрузка" })).toBeNull();
  });
});
