import { Link, useLocation } from "react-router";

import { AppIcon } from "../../shared/components/app-icon";

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
                <AppIcon name={item.key === "create" ? "add" : item.key} />
              </span>
              <span className="primary-navigation__label">{item.label}</span>
            </Link>
          );
        })}
      </div>
    </nav>
  );
}
