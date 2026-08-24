// @vitest-environment jsdom

import { cleanup, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { Budget } from "../../shared/api/types";
import { formatMoney } from "../../shared/finance/money";
import { BudgetCard } from "./budget-card";
import {
  BUDGET_STATE_PRESENTATION,
  BudgetProgressOverview,
  BudgetStateLegend,
} from "./budget-progress";
import type { BudgetProgressState } from "./budget-progress";

vi.mock("../../shared/auth/use-session-format", () => ({
  useSessionFormat: () => ({
    baseCurrency: "RUB",
    locale: "ru-RU",
    timeZone: "Europe/Moscow",
  }),
}));

function budgetFixture(
  state: BudgetProgressState,
  progress: Partial<Budget["progress"]> = {},
): Budget {
  return {
    category_id: null,
    created_at: "2026-08-01T00:00:00.000Z",
    currency: "RUB",
    deleted_at: null,
    ends_on: "2026-08-31",
    id: `018f0000-0000-7000-8000-0000000000${state === "on_track" ? "10" : state === "watch" ? "11" : "12"}`,
    limit_minor: "100000",
    name: "Основной бюджет",
    progress: {
      cutoff_at: "2026-08-24T12:00:00.000Z",
      forecast_minor: "75000",
      known_recurring_minor: "25000",
      measured_at: "2026-08-24T12:00:00.000Z",
      overspent_minor: state === "over" ? "5000" : "0",
      progress_bps: state === "over" ? 10_000 : 5_000,
      remaining_minor: state === "over" ? "0" : "50000",
      safe_daily_minor: state === "on_track" ? "3125" : "0",
      spent_minor: state === "over" ? "105000" : "50000",
      state,
      ...progress,
    },
    starts_on: "2026-08-01",
    timezone: "Europe/Moscow",
    updated_at: "2026-08-24T12:00:00.000Z",
    version: 3,
  };
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("budget progress presentation", () => {
  it.each<readonly [BudgetProgressState, string]>([
    ["on_track", "В плане"],
    ["watch", "Нужен запас"],
    ["over", "Лимит превышен"],
  ])("renders authoritative %s state without deriving it from percentage", (state, label) => {
    const budget = budgetFixture(state, { progress_bps: state === "watch" ? 500 : 9_900 });
    render(
      <MemoryRouter>
        <BudgetCard budget={budget} />
      </MemoryRouter>,
    );

    const card = screen.getByRole("link", { name: /Основной бюджет/u });
    expect(card.getAttribute("data-state")).toBe(state);
    expect(screen.getByLabelText(`Статус бюджета: ${label}`).getAttribute("data-state")).toBe(state);
    expect(screen.getByText(BUDGET_STATE_PRESENTATION[state].description)).not.toBeNull();
    expect(screen.getByText("Фактически потрачено")).not.toBeNull();
    expect(screen.getByText("Известные регулярные")).not.toBeNull();
    expect(screen.getByText("Прогноз к концу периода")).not.toBeNull();
    expect(screen.getByText("Безопасно в день")).not.toBeNull();
    expect(screen.getByText(state === "over" ? "Перерасход" : "Остаток лимита")).not.toBeNull();
  });

  it.each([320, 390])(
    "keeps exact oversized minor strings and compact semantics at %ipx",
    (width) => {
      Object.defineProperty(window, "innerWidth", { configurable: true, value: width });
      const spent = "900719925474099312345";
      const recurring = "900719925474099312346";
      const forecast = "1801439850948198624691";
      const safeDaily = "900719925474099312347";
      const budget = budgetFixture("watch", {
        forecast_minor: forecast,
        known_recurring_minor: recurring,
        safe_daily_minor: safeDaily,
        spent_minor: spent,
      });
      render(<BudgetProgressOverview budget={budget} locale="en-US" />);

      expect(screen.getByText(formatMoney(spent, "RUB", "en-US"))).not.toBeNull();
      expect(screen.getByText(formatMoney(recurring, "RUB", "en-US"))).not.toBeNull();
      expect(screen.getByText(formatMoney(forecast, "RUB", "en-US"))).not.toBeNull();
      expect(screen.getByText(formatMoney(safeDaily, "RUB", "en-US"))).not.toBeNull();
      const metrics = document.querySelector(".budget-progress-metrics");
      expect(metrics?.getAttribute("data-mobile-layout")).toBe("stack-to-grid");
      expect(metrics?.querySelectorAll(":scope > div")).toHaveLength(5);
      for (const amount of metrics?.querySelectorAll("dd") ?? []) {
        expect(amount.getAttribute("style")).toBeNull();
      }
    },
  );

  it("provides a visible three-state legend", () => {
    render(<BudgetStateLegend />);
    const legend = screen.getByLabelText("Легенда статусов бюджета");
    expect(legend.querySelectorAll(".budget-state-legend__item")).toHaveLength(3);
    expect(screen.getByText("В плане")).not.toBeNull();
    expect(screen.getByText("Нужен запас")).not.toBeNull();
    expect(screen.getByText("Лимит превышен")).not.toBeNull();
  });
});
