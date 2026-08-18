import type { ReactNode } from "react";

interface PageHeadingProps {
  readonly eyebrow: string;
  readonly title: string;
  readonly description?: string;
  readonly action?: ReactNode;
}
export function PageHeading({ eyebrow, title, description, action }: PageHeadingProps) {
  return (
    <header className="page-heading">
      <div>
        <p className="eyebrow">{eyebrow}</p>
        <h1 className="page-title">{title}</h1>
        {description === undefined ? null : <p className="page-description">{description}</p>}
      </div>
      {action === undefined ? null : <div className="page-heading__action">{action}</div>}
    </header>
  );
}
