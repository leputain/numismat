import { Component } from "react";
import type { ErrorInfo, PropsWithChildren, ReactNode } from "react";

import { emitClientEvent } from "../shared/logging/client-events";

interface AppErrorBoundaryState {
  hasError: boolean;
}

export class AppErrorBoundary extends Component<PropsWithChildren, AppErrorBoundaryState> {
  public override state: AppErrorBoundaryState = { hasError: false };

  public static getDerivedStateFromError(): AppErrorBoundaryState {
    return { hasError: true };
  }

  public override componentDidCatch(_error: Error, _errorInfo: ErrorInfo): void {
    emitClientEvent("ui_unexpected_error");
    queueMicrotask(() => document.getElementById("fatal-error-heading")?.focus());
  }

  public override render(): ReactNode {
    if (this.state.hasError) {
      return (
        <main className="grid min-h-dvh place-items-center bg-[#081012] px-6 text-stone-100">
          <section className="max-w-md rounded-3xl border border-white/10 bg-white/5 p-7 text-center" role="alert">
            <h1 className="font-serif text-2xl" id="fatal-error-heading" tabIndex={-1}>Интерфейс временно недоступен</h1>
            <p className="mt-3 text-sm leading-6 text-stone-400">
              Закройте Mini App и откройте его снова. Детали ошибки не сохранялись.
            </p>
          </section>
        </main>
      );
    }

    return this.props.children;
  }
}
