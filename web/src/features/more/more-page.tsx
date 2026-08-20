import { useEffect } from "react";
import { Link } from "react-router";

import { PageHeading } from "../../shared/components/page-heading";
import { AppIcon } from "../../shared/components/app-icon";
import { emitClientEvent } from "../../shared/logging/client-events";

const TOOL_GROUPS = [
  {
    id: "planning",
    title: "Планирование",
    items: [
      {
        to: "/budgets",
        icon: "budget",
        title: "Бюджеты",
        description: "Лимиты расходов по периоду и валюте",
      },
      {
        to: "/recurring",
        icon: "recurring",
        title: "Регулярные операции",
        description: "Расписания с обязательной проверкой черновика",
      },
    ],
  },
  {
    id: "data",
    title: "Данные",
    items: [
      {
        to: "/rates",
        icon: "rates",
        title: "Курсы валют",
        description: "Версии курсов для явного пересчёта отчётов",
      },
      {
        to: "/imports",
        icon: "import",
        title: "Банковский импорт",
        description: "Загрузка и разбор строк перед записью",
      },
    ],
  },
] as const;

export function MorePage() {
  useEffect(() => emitClientEvent("more_opened"), []);

  return (
    <div className="page-stack">
      <PageHeading
        description="Редкие инструменты собраны отдельно, чтобы основная навигация оставалась спокойной."
        eyebrow="Управление"
        title="Все инструменты"
      />

      {TOOL_GROUPS.map((group) => (
        <section aria-labelledby={`tools-${group.id}`} key={group.id}>
          <div className="section-heading">
            <div>
              <p className="eyebrow">Раздел</p>
              <h2 className="section-title" id={`tools-${group.id}`}>{group.title}</h2>
            </div>
          </div>
          <div className="tool-grid">
            {group.items.map((item) => (
              <Link className="tool-card" key={item.to} to={item.to}>
                <span aria-hidden="true" className="tool-card__icon"><AppIcon name={item.icon} /></span>
                <span className="tool-card__body">
                  <strong>{item.title}</strong>
                  <span>{item.description}</span>
                </span>
                <span aria-hidden="true" className="tool-card__chevron"><AppIcon name="chevron-right" /></span>
              </Link>
            ))}
          </div>
        </section>
      ))}

      <aside className="privacy-note">
        <span aria-hidden="true" className="privacy-note__icon"><AppIcon name="shield" /></span>
        <div>
          <strong>Запись только после проверки</strong>
          <p>Черновики, импорт и регулярные операции не создают финансовую запись без вашего подтверждения.</p>
        </div>
      </aside>
    </div>
  );
}
