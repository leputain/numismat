import { Link, useLocation } from "react-router";

type NavigationKey = "overview" | "transactions" | "create" | "analytics" | "more";

const NAVIGATION = [
  { key: "overview", to: "/", label: "Обзор" },
  { key: "transactions", to: "/transactions", label: "Операции" },
  { key: "create", to: "/draft", label: "Добавить" },
  { key: "analytics", to: "/analytics", label: "Аналитика" },
  { key: "more", to: "/more", label: "Ещё" },
] as const satisfies ReadonlyArray<{
  readonly key: NavigationKey;
  readonly to: string;
  readonly label: string;
}>;

function isActiveNavigation(key: NavigationKey, pathname: string): boolean {
  if (key === "overview") {
    return pathname === "/";
  }
  if (key === "transactions") {
    return pathname.startsWith("/transactions");
  }
  if (key === "create") {
    return pathname.startsWith("/draft");
  }
  if (key === "analytics") {
    return pathname.startsWith("/analytics");
  }
  return ["/more", "/budgets", "/recurring", "/rates", "/imports"].some(
    (prefix) => pathname === prefix || pathname.startsWith(`${prefix}/`),
  );
}

function NavigationIcon({ name }: { readonly name: NavigationKey }) {
  if (name === "create") {
    return (
      <svg aria-hidden="true" viewBox="0 0 24 24">
        <path d="M12 5v14M5 12h14" />
      </svg>
    );
  }
  if (name === "overview") {
    return (
      <svg aria-hidden="true" viewBox="0 0 24 24">
        <path d="M4 11.5 12 5l8 6.5V20h-5v-5H9v5H4z" />
      </svg>
    );
  }
  if (name === "transactions") {
    return (
      <svg aria-hidden="true" viewBox="0 0 24 24">
        <path d="M6 7h12M6 12h12M6 17h8" />
        <path d="m16 15 3 2-3 2" />
      </svg>
    );
  }
  if (name === "analytics") {
    return (
      <svg aria-hidden="true" viewBox="0 0 24 24">
        <path d="M5 19V9M12 19V5M19 19v-7" />
      </svg>
    );
  }
  return (
    <svg aria-hidden="true" viewBox="0 0 24 24">
      <circle cx="7" cy="7" r="1.5" />
      <circle cx="17" cy="7" r="1.5" />
      <circle cx="7" cy="17" r="1.5" />
      <circle cx="17" cy="17" r="1.5" />
    </svg>
  );
}

export function PrimaryNavigation() {
  const { pathname } = useLocation();
  return (
    <nav aria-label="Основная навигация" className="primary-navigation">
      <div className="primary-navigation__items">
        {NAVIGATION.map((item) => {
          const active = isActiveNavigation(item.key, pathname);
          return (
            <Link
              aria-current={active ? "page" : undefined}
              aria-label={item.key === "create" ? "Добавить новую операцию" : item.label}
              className={`primary-navigation__link${active ? " is-active" : ""}${
                item.key === "create" ? " primary-navigation__link--create" : ""
              }`}
              key={item.key}
              to={item.to}
            >
              <span className="primary-navigation__icon">
                <NavigationIcon name={item.key} />
              </span>
              <span className="primary-navigation__label">{item.label}</span>
            </Link>
          );
        })}
      </div>
    </nav>
  );
}
