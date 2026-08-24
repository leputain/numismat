import { Component } from "react";
import type { ErrorInfo, PropsWithChildren, ReactNode } from "react";

import { emitClientEvent } from "../shared/logging/client-events";

interface AppErrorBoundaryState {
  errorKind: "chunk" | "unexpected" | undefined;
}

interface AppErrorBoundaryProps extends PropsWithChildren {
  readonly reloadPage?: () => void;
}

const CHUNK_ERROR_MARKERS = [
  "chunkloaderror",
  "failed to fetch dynamically imported module",
  "error loading dynamically imported module",
  "importing a module script failed",
  "loading chunk",
] as const;

export function classifyUiError(error: unknown): "chunk" | "unexpected" {
  if (!(error instanceof Error)) {
    return "unexpected";
  }
  const fingerprint = `${error.name} ${error.message}`.toLowerCase();
  return CHUNK_ERROR_MARKERS.some((marker) => fingerprint.includes(marker))
    ? "chunk"
    : "unexpected";
}

export class AppErrorBoundary extends Component<
  AppErrorBoundaryProps,
  AppErrorBoundaryState
> {
  public override state: AppErrorBoundaryState = { errorKind: undefined };

  public static getDerivedStateFromError(error: unknown): AppErrorBoundaryState {
    return { errorKind: classifyUiError(error) };
  }

  public override componentDidCatch(_error: Error, _errorInfo: ErrorInfo): void {
    emitClientEvent("ui_unexpected_error");
    queueMicrotask(() => document.getElementById("fatal-error-heading")?.focus());
  }

  public override render(): ReactNode {
    if (this.state.errorKind !== undefined) {
      const chunkFailed = this.state.errorKind === "chunk";
      return (
        <main className="system-state">
          <section className="system-state__card system-state__card--bordered" role="alert">
            <h1 id="fatal-error-heading" tabIndex={-1}>
              {chunkFailed ? "Нужно обновить интерфейс" : "Интерфейс временно недоступен"}
            </h1>
            <p>
              {chunkFailed
                ? "После обновления приложения осталась старая версия экрана."
                : "Произошёл локальный сбой. Операции не повторяются автоматически."}
            </p>
            <button
              className="button button--primary system-state__action"
              onClick={this.#reloadPage}
              type="button"
            >
              Перезагрузить
            </button>
          </section>
        </main>
      );
    }

    return this.props.children;
  }

  readonly #reloadPage = (): void => {
    if (this.props.reloadPage !== undefined) {
      this.props.reloadPage();
      return;
    }
    window.location.reload();
  };
}
