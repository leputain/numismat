import { describe, expect, it } from "vitest";

import type { Draft } from "../../shared/api/types";
import { draftProgressFor } from "./draft-progress";

function projectedDraft(
  flow: Draft["flow"],
  state: Draft["state"],
  amountMinor: string | null,
): Pick<Draft, "flow" | "state" | "transaction"> {
  return {
    flow,
    state,
    transaction: amountMinor === null ? null : { amount_minor: amountMinor },
  };
}

describe("draft progress projection", () => {
  it("maps amount-only quick capture to five steps without changing public flow", () => {
    const expectations = [
      ["wizard_type", 0],
      ["wizard_category", 1],
      ["wizard_account", 2],
      ["wizard_date", 3],
      ["wizard_description", 4],
      ["wizard_confirm", 4],
    ] as const;

    for (const [state, currentIndex] of expectations) {
      const progress = draftProgressFor(projectedDraft("quick", state, "50000"));
      expect(progress?.steps).toHaveLength(5);
      expect(progress?.currentIndex).toBe(currentIndex);
    }
    expect(
      draftProgressFor(projectedDraft("quick", "wizard_description", "50000"))?.steps[4],
    ).toBe("Описание");
    expect(
      draftProgressFor(projectedDraft("quick", "wizard_confirm", "50000"))?.steps[4],
    ).toBe("Проверка");
  });

  it("leaves the full wizard and regular quick progress unchanged", () => {
    expect(draftProgressFor(projectedDraft("wizard", "wizard_type", null))?.steps).toHaveLength(7);
    expect(draftProgressFor(projectedDraft("wizard", "wizard_confirm", "50000"))?.currentIndex).toBe(6);
    expect(draftProgressFor(projectedDraft("quick", "quick_account", "50000"))?.steps).toEqual([
      "Категория",
      "Счёт",
      "Проверка",
    ]);
  });
});
