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
    <section className="route-state" role="alert">
      <span aria-hidden="true" className="route-state__mark">404</span>
      <p className="eyebrow">Здесь ничего нет</p>
      <h1>{title}</h1>
      <p>{description}</p>
      <Link
        className="button button--secondary route-state__action"
        to="/"
      >
        На главную
      </Link>
    </section>
  );
}
