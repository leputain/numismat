import { describe, expect, it, vi } from "vitest";

import { scrollToPageStart } from "./app-shell";

describe("scrollToPageStart", () => {
  it("contains a native WebView scroll failure during route changes", () => {
    const scrollTo = vi.fn(() => {
      throw new Error("sentinel-scroll-failure");
    });

    expect(() => scrollToPageStart({ scrollTo })).not.toThrow();
    expect(scrollTo).toHaveBeenCalledWith(0, 0);
  });
});
