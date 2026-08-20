import { useEffect, useRef } from "react";

interface AuthErrorStateProps {
  readonly title: string;
  readonly description: string;
  readonly actionLabel: string;
  readonly onAction: () => void;
}

export function AuthErrorState({
  title,
  description,
  actionLabel,
  onAction,
}: AuthErrorStateProps) {
  const headingRef = useRef<HTMLHeadingElement>(null);
  useEffect(() => headingRef.current?.focus(), []);

  return (
    <main className="system-state">
      <section
        className="system-state__card system-state__card--bordered"
        role="alert"
      >
        <p className="eyebrow">
          Защищённый контур
        </p>
        <h1 ref={headingRef} tabIndex={-1}>
          {title}
        </h1>
        <p>{description}</p>
        <button
          className="button button--primary system-state__action"
          onClick={onAction}
          type="button"
        >
          {actionLabel}
        </button>
      </section>
    </main>
  );
}
