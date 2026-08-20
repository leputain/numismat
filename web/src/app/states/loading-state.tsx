interface LoadingStateProps {
  readonly message?: string;
}

export function LoadingState({ message = "Проверяем защищённую сессию…" }: LoadingStateProps) {
  return (
    <main
      aria-busy="true"
      className="system-state"
    >
      <section className="system-state__card" role="status">
        <div aria-hidden="true" className="system-state__loader" />
        <h1>Numismat</h1>
        <p>{message}</p>
      </section>
    </main>
  );
}
