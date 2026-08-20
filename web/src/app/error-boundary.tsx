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
        <main className="system-state">
          <section className="system-state__card system-state__card--bordered" role="alert">
            <h1 id="fatal-error-heading" tabIndex={-1}>Интерфейс временно недоступен</h1>
            <p>
              Закройте Mini App и откройте его снова. Детали ошибки не сохранялись.
            </p>
          </section>
        </main>
      );
    }

    return this.props.children;
  }
}
