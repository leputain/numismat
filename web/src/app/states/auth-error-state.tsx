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
    <main className="grid min-h-[var(--tg-viewport-stable-height,100dvh)] place-items-center px-6 text-stone-100">
      <section
        className="w-full max-w-md rounded-3xl border border-white/10 bg-[#0c1416]/92 p-7 text-center shadow-2xl"
        role="alert"
      >
        <p className="text-xs font-semibold uppercase tracking-[0.2em] text-amber-200/70">
          Защищённый контур
        </p>
        <h1 className="mt-3 font-serif text-3xl" ref={headingRef} tabIndex={-1}>
          {title}
        </h1>
        <p className="mt-4 text-sm leading-6 text-stone-400">{description}</p>
        <button
          className="mt-6 min-h-11 w-full rounded-2xl bg-stone-100 px-5 py-3 font-semibold text-stone-950 outline-none transition-colors hover:bg-white focus-visible:ring-2 focus-visible:ring-amber-200 focus-visible:ring-offset-2 focus-visible:ring-offset-[#0c1416]"
          onClick={onAction}
          type="button"
        >
          {actionLabel}
        </button>
      </section>
    </main>
  );
}
