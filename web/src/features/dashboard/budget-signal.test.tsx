// @vitest-environment jsdom

import { cleanup, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { afterEach, describe, expect, it } from "vitest";

import type { Budget } from "../../shared/api/types";
import { formatMoney } from "../../shared/finance/money";
import { BUDGET_STATE_PRESENTATION } from "../budgets/budget-progress";
import type { BudgetProgressState } from "../budgets/budget-progress";
import { DashboardBudgetSignal } from "./budget-signal";

function budgetFixture(
  idSuffix: string,
  state: BudgetProgressState,
  progressBps: number,
): Budget {
  return {
    category_id: null,
    created_at: "2026-08-01T00:00:00.000Z",
    currency: "RUB",
    deleted_at: null,
    ends_on: "2026-08-31",
    id: `018f0000-0000-7000-8000-0000000000${idSuffix}`,
    limit_minor: "100000",
    name: `${BUDGET_STATE_PRESENTATION[state].label} бюджет`,
    progress: {
      cutoff_at: "2026-08-24T12:00:00.000Z",
      forecast_minor: "90000",
      known_recurring_minor: "20000",
      measured_at: "2026-08-24T12:00:00.000Z",
      overspent_minor: state === "over" ? "1000" : "0",
      progress_bps: progressBps,
      remaining_minor: state === "over" ? "0" : "30000",
      safe_daily_minor: state === "on_track" ? "1250" : "0",
      spent_minor: state === "over" ? "101000" : "70000",
      state,
    },
    starts_on: "2026-08-01",
    timezone: "Europe/Moscow",
    updated_at: "2026-08-24T12:00:00.000Z",
    version: 1,
  };
}

afterEach(cleanup);

describe("dashboard budget signal", () => {
  it("prioritizes backend state over progress percentage", () => {
    const onTrack = budgetFixture("10", "on_track", 9_900);
    const watch = budgetFixture("11", "watch", 100);
    render(
      <MemoryRouter>
        <DashboardBudgetSignal budgets={[onTrack, watch]} locale="ru-RU" />
      </MemoryRouter>,
    );

    expect(screen.getByText("Регулярные расходы требуют внимания")).not.toBeNull();
    expect(screen.queryByText("Расходы под контролем")).toBeNull();
    expect(screen.getByRole("link", { name: "Подробнее" }).getAttribute("href")).toBe(
      `/budgets/${watch.id}`,
    );
    const expenses = screen.getByRole("link", { name: "Показать расходы" });
    expect(expenses.getAttribute("href")).toContain("/transactions?period=custom");
    expect(expenses.getAttribute("href")).toContain("type=expense");
    expect(expenses.getAttribute("href")).toContain("currency=RUB");
    expect(document.querySelector(".budget-signal")?.getAttribute("data-state")).toBe("watch");
  });

  it("fails closed when the bounded dashboard page is incomplete", () => {
    render(
      <MemoryRouter>
        <DashboardBudgetSignal
          budgets={[budgetFixture("14", "on_track", 100)]}
          complete={false}
          locale="ru-RU"
        />
      </MemoryRouter>,
    );

    expect(screen.getByText("Проверьте список бюджетов")).not.toBeNull();
    expect(screen.queryByText("Расходы под контролем")).toBeNull();
    expect(screen.getByRole("link", { name: "Все бюджеты" }).getAttribute("href")).toBe(
      "/budgets",
    );
  });

  it.each<readonly [BudgetProgressState, string]>([
    ["on_track", "Расходы под контролем"],
    ["watch", "Регулярные расходы требуют внимания"],
    ["over", "Лимит уже превышен"],
  ])("renders the %s signal copy", (state, title) => {
    render(
      <MemoryRouter>
        <DashboardBudgetSignal budgets={[budgetFixture("12", state, 5_000)]} locale="ru-RU" />
      </MemoryRouter>,
    );
    expect(screen.getByText(title)).not.toBeNull();
    expect(document.querySelector(".budget-signal")?.getAttribute("data-state")).toBe(state);
  });

  it("formats oversized safe-daily and recurring values without Number conversion", () => {
    const budget = budgetFixture("13", "watch", 5_000);
    budget.progress.safe_daily_minor = "900719925474099312345";
    budget.progress.known_recurring_minor = "900719925474099312346";
    render(
      <MemoryRouter>
        <DashboardBudgetSignal budgets={[budget]} locale="en-US" />
      </MemoryRouter>,
    );

    expect(
      screen.getByText(formatMoney("900719925474099312345", "RUB", "en-US")),
    ).not.toBeNull();
    expect(
      screen.getByText(formatMoney("900719925474099312346", "RUB", "en-US")),
    ).not.toBeNull();
    expect(screen.getByText("Каждая валюта считается отдельно.", { exact: false })).not.toBeNull();
  });
});
