// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { Transaction } from "../../shared/api/types";
import { DraftCaptureLanding } from "./draft-capture-landing";
import { QuickCaptureForm } from "./quick-capture-form";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

function transaction(index: number): Transaction {
  const suffix = String(index + 1).padStart(12, "0");
  return {
    account: { id: `10000000-0000-0000-0000-${suffix}`, name: `Счёт ${String(index + 1)}` },
    amount_minor: String((index + 1) * 10_000),
    category: {
      emoji: "●",
      id: `20000000-0000-0000-0000-${suffix}`,
      name: `Категория ${String(index + 1)}`,
    },
    currency: "RUB",
    deleted_at: null,
    description: "",
    id: `30000000-0000-0000-0000-${suffix}`,
    occurred_at: "2026-08-24T09:00:00+03:00",
    source: "manual",
    type: index % 2 === 0 ? "expense" : "income",
    version: index + 1,
  };
}

const IDLE_MUTATION = {
  error: null,
  onRetryUnknown: () => undefined,
  outcomeUnknown: false,
  pending: false,
} as const;

describe("draft capture landing", () => {
  it("submits trimmed quick input, limits recent rows to three and exposes review-first actions", () => {
    const onQuickSubmit = vi.fn();
    const onRepeat = vi.fn();
    const onStartWizard = vi.fn();
    const transactions = [transaction(0), transaction(1), transaction(2), transaction(3)];

    render(
      <MemoryRouter>
        <DraftCaptureLanding
          actionsDisabled={false}
          locale="ru-RU"
          onQuickSubmit={onQuickSubmit}
          onRepeat={onRepeat}
          onRetryRecent={() => undefined}
          onStartWizard={onStartWizard}
          quickMutation={IDLE_MUTATION}
          recentError={false}
          recentPending={false}
          recentTransactions={transactions}
          repeatMutation={IDLE_MUTATION}
          timeZone="Europe/Moscow"
        />
      </MemoryRouter>,
    );

    fireEvent.change(screen.getByLabelText("Сумма или короткая запись"), {
      target: { value: "  1 200,50  " },
    });
    fireEvent.click(screen.getByRole("button", { name: "Продолжить" }));
    expect(onQuickSubmit).toHaveBeenCalledWith("1 200,50");

    const repeatButtons = screen.getAllByRole("button", { name: /Повторить операцию:/u });
    expect(repeatButtons).toHaveLength(3);
    expect(screen.queryByText("Категория 4")).toBeNull();
    fireEvent.click(repeatButtons[0]!);
    expect(onRepeat).toHaveBeenCalledWith(transactions[0]);

    fireEvent.click(screen.getByRole("button", { name: "Пошаговый ввод" }));
    expect(onStartWizard).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("link", { name: "Все поля сразу" }).getAttribute("href")).toBe(
      "/draft/compose",
    );
    expect(screen.getByRole("button", { name: "Продолжить" }).closest("[data-mobile-sticky='true']")).not.toBeNull();
    expect(screen.getByText(/Ничего не сохранится без проверки/u)).not.toBeNull();
  });

  it.each([320, 390, 768])(
    "keeps focus and sticky-action semantics safe at %ipx",
    (width) => {
      Object.defineProperty(window, "innerWidth", { configurable: true, value: width });
      Object.defineProperty(window, "matchMedia", {
        configurable: true,
        value: vi.fn((query: string) => ({
          matches: query.includes("pointer: coarse") && width <= 768,
          media: query,
          onchange: null,
          addEventListener: vi.fn(),
          removeEventListener: vi.fn(),
          addListener: vi.fn(),
          removeListener: vi.fn(),
          dispatchEvent: vi.fn(),
        } satisfies MediaQueryList)),
      });
      const scrollIntoView = vi.fn();
      Object.defineProperty(HTMLElement.prototype, "scrollIntoView", {
        configurable: true,
        value: scrollIntoView,
      });
      vi.spyOn(window, "requestAnimationFrame").mockImplementation((callback) => {
        callback(0);
        return 1;
      });
      vi.spyOn(window, "cancelAnimationFrame").mockImplementation(() => undefined);

      render(<QuickCaptureForm busy={false} disabled={false} onSubmit={() => undefined} />);

      const input = screen.getByLabelText("Сумма или короткая запись");
      expect(document.activeElement).toBe(input);
      expect(scrollIntoView).toHaveBeenCalledWith({
        behavior: "auto",
        block: "nearest",
        inline: "nearest",
      });
      expect(screen.getByRole("button", { name: "Продолжить" }).closest("[data-mobile-sticky='true']")).not.toBeNull();
    },
  );
});
