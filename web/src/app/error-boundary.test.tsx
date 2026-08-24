// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { AppErrorBoundary } from "./error-boundary";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

function BrokenLazyPage(): never {
  throw new TypeError("Failed to fetch dynamically imported module");
}

describe("AppErrorBoundary", () => {
  it("offers a recoverable reload when a lazy page chunk fails", () => {
    vi.spyOn(console, "error").mockImplementation(() => undefined);
    const reloadPage = vi.fn();

    render(
      <AppErrorBoundary reloadPage={reloadPage}>
        <BrokenLazyPage />
      </AppErrorBoundary>,
    );

    expect(
      screen.getByRole("heading", { name: "Нужно обновить интерфейс" }),
    ).not.toBeNull();
    expect(
      screen.getByText("После обновления приложения осталась старая версия экрана."),
    ).not.toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Перезагрузить" }));
    expect(reloadPage).toHaveBeenCalledOnce();
  });
});
