interface LoadingStateProps {
  readonly message?: string;
}

export function LoadingState({ message = "Проверяем защищённую сессию…" }: LoadingStateProps) {
  return (
    <main
      aria-busy="true"
      className="grid min-h-[var(--tg-viewport-stable-height,100dvh)] place-items-center px-6 text-stone-100"
    >
      <section className="max-w-sm text-center" role="status">
        <div aria-hidden="true" className="mx-auto mb-5 size-10 rounded-full border border-amber-200/20 bg-amber-200/8" />
        <h1 className="font-serif text-2xl">Numismat</h1>
        <p className="mt-3 text-sm leading-6 text-stone-400">{message}</p>
      </section>
    </main>
  );
}
