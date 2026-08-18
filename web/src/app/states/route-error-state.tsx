import { Link } from "react-router";

interface RouteErrorStateProps {
  readonly title?: string;
  readonly description?: string;
}

export function RouteErrorState({
  title = "Страница не найдена",
  description = "Этот адрес не относится к доступным разделам Numismat.",
}: RouteErrorStateProps) {
  return (
    <section className="mx-auto grid max-w-md place-items-center py-20 text-center" role="alert">
      <p className="text-xs font-semibold uppercase tracking-[0.2em] text-stone-500">Ошибка маршрута</p>
      <h1 className="mt-3 font-serif text-3xl text-stone-50">{title}</h1>
      <p className="mt-4 text-sm leading-6 text-stone-400">{description}</p>
      <Link
        className="mt-6 grid min-h-11 min-w-44 place-items-center rounded-2xl border border-white/12 bg-white/7 px-5 text-sm font-semibold text-stone-100 outline-none focus-visible:ring-2 focus-visible:ring-amber-200"
        to="/"
      >
        На главную
      </Link>
    </section>
  );
}
