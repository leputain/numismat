import { useEffect, useRef, useState } from "react";

const QUICK_INPUT_MAX_LENGTH = 4096;
const MOBILE_CAPTURE_QUERY = "(max-width: 768px) and (pointer: coarse)";

export interface QuickCaptureFormProps {
  readonly busy: boolean;
  readonly disabled: boolean;
  onSubmit(text: string): void;
}

function matchesMobileCaptureViewport(): boolean {
  return (
    typeof window !== "undefined" &&
    typeof window.matchMedia === "function" &&
    window.matchMedia(MOBILE_CAPTURE_QUERY).matches
  );
}

/**
 * Shared amount/text entry UI. Transport and idempotency stay in
 * useQuickDraftMutation so another screen cannot accidentally issue a raw POST.
 */
export function QuickCaptureForm({ busy, disabled, onSubmit }: QuickCaptureFormProps) {
  const [text, setText] = useState("");
  const inputRef = useRef<HTMLInputElement>(null);
  const scrollFrameRef = useRef<number | undefined>(undefined);

  const revealInput = () => {
    if (!matchesMobileCaptureViewport() || document.visibilityState !== "visible") {
      return;
    }
    if (scrollFrameRef.current !== undefined) {
      window.cancelAnimationFrame(scrollFrameRef.current);
    }
    scrollFrameRef.current = window.requestAnimationFrame(() => {
      scrollFrameRef.current = undefined;
      try {
        inputRef.current?.scrollIntoView?.({
          behavior: "auto",
          block: "nearest",
          inline: "nearest",
        });
      } catch {
        // Older WebViews may not accept ScrollIntoViewOptions; do not force a jumping fallback.
      }
    });
  };

  useEffect(() => {
    if (!matchesMobileCaptureViewport() || document.visibilityState !== "visible") {
      return undefined;
    }
    const focusFrame = window.requestAnimationFrame(() => {
      try {
        inputRef.current?.focus({ preventScroll: true });
      } catch {
        // Keeping the viewport stable is more important than forcing focus in an old WebView.
      }
    });
    return () => {
      window.cancelAnimationFrame(focusFrame);
      if (scrollFrameRef.current !== undefined) {
        window.cancelAnimationFrame(scrollFrameRef.current);
      }
    };
  }, []);

  const normalized = text.trim();
  return (
    <form
      className="quick-capture-form"
      onSubmit={(event) => {
        event.preventDefault();
        if (normalized.length > 0) {
          onSubmit(normalized);
        }
      }}
    >
      <label className="field-label" htmlFor="quick-capture-input">
        Сумма или короткая запись
      </label>
      <input
        aria-describedby="quick-capture-help"
        autoComplete="off"
        autoCorrect="off"
        className="field-input quick-capture-input"
        disabled={disabled}
        enterKeyHint="done"
        id="quick-capture-input"
        maxLength={QUICK_INPUT_MAX_LENGTH}
        onChange={(event) => setText(event.currentTarget.value)}
        onFocus={revealInput}
        placeholder="500 или 450 кофе"
        ref={inputRef}
        spellCheck={false}
        type="text"
        value={text}
      />
      <p className="quick-capture-help" id="quick-capture-help">
        Можно ввести только сумму — тип, категорию и счёт выберете дальше. Ничего не
        сохранится без проверки.
      </p>
      <div className="quick-capture__actions" data-mobile-sticky="true">
        <button
          className="button button--primary quick-capture__submit"
          disabled={disabled || normalized.length === 0}
          type="submit"
        >
          {busy ? "Обрабатываем…" : "Продолжить"}
        </button>
      </div>
    </form>
  );
}
