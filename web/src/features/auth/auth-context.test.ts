import { describe, expect, it, vi } from "vitest";

import type { AuthState } from "./auth-coordinator";
import { installAuthPageLifecycle } from "./auth-context";

class VisibilityTarget {
  readonly #events = new EventTarget();
  public visibilityState: DocumentVisibilityState = "visible";

  public addEventListener(type: "visibilitychange", listener: EventListener): void {
    this.#events.addEventListener(type, listener);
  }

  public removeEventListener(type: "visibilitychange", listener: EventListener): void {
    this.#events.removeEventListener(type, listener);
  }

  public changeTo(visibilityState: DocumentVisibilityState): void {
    this.visibilityState = visibilityState;
    this.#events.dispatchEvent(new Event("visibilitychange"));
  }
}

class PageTarget {
  readonly #events = new EventTarget();

  public addEventListener(type: "pagehide" | "pageshow", listener: EventListener): void {
    this.#events.addEventListener(type, listener);
  }

  public removeEventListener(type: "pagehide" | "pageshow", listener: EventListener): void {
    this.#events.removeEventListener(type, listener);
  }

  public dispatch(type: "pagehide" | "pageshow", persisted: boolean): void {
    const event = new Event(type);
    Object.defineProperty(event, "persisted", { value: persisted });
    this.#events.dispatchEvent(event);
  }
}

describe("installAuthPageLifecycle", () => {
  it("suspends an initially hidden page before it can expose protected state", () => {
    const visibility = new VisibilityTarget();
    visibility.visibilityState = "hidden";
    const page = new PageTarget();
    const suspendProtectedSession = vi.fn();
    const recheckAuthenticatedSession = vi.fn(async () => ({ status: "booting" }) as AuthState);

    installAuthPageLifecycle(
      { recheckAuthenticatedSession, suspendProtectedSession },
      visibility,
      page,
    );

    expect(suspendProtectedSession).toHaveBeenCalledOnce();
    visibility.changeTo("visible");
    expect(recheckAuthenticatedSession).toHaveBeenCalledOnce();
  });

  it("hides protected state synchronously and restores it once through a session check", () => {
    const visibility = new VisibilityTarget();
    const page = new PageTarget();
    let state: AuthState = {
      status: "authenticated",
      session: {
        locale: "ru",
        timezone: "Europe/Moscow",
        baseCurrency: "RUB",
        settingsVersion: 1,
        expiresAt: "2030-01-01T00:00:00Z",
      },
    };
    const suspendProtectedSession = vi.fn(() => {
      state = { status: "booting" };
    });
    const recheckAuthenticatedSession = vi.fn(async () => state);
    const uninstall = installAuthPageLifecycle(
      { recheckAuthenticatedSession, suspendProtectedSession },
      visibility,
      page,
    );

    visibility.changeTo("hidden");
    expect(state.status).toBe("booting");
    expect(suspendProtectedSession).toHaveBeenCalledOnce();

    page.dispatch("pagehide", false);
    expect(suspendProtectedSession).toHaveBeenCalledOnce();

    visibility.changeTo("visible");
    expect(recheckAuthenticatedSession).toHaveBeenCalledOnce();

    page.dispatch("pageshow", true);
    expect(recheckAuthenticatedSession).toHaveBeenCalledOnce();

    uninstall();
    visibility.changeTo("hidden");
    expect(suspendProtectedSession).toHaveBeenCalledOnce();
  });
});
