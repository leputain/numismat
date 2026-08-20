type AppIconName =
  | "add"
  | "analytics"
  | "budget"
  | "chevron-right"
  | "import"
  | "more"
  | "overview"
  | "rates"
  | "recurring"
  | "shield"
  | "transactions";

interface AppIconProps {
  readonly name: AppIconName;
  readonly className?: string;
}

export function AppIcon({ name, className }: AppIconProps) {
  const common = {
    "aria-hidden": true,
    className,
    viewBox: "0 0 24 24",
  } as const;

  if (name === "add") {
    return <svg {...common}><path d="M12 5v14M5 12h14" /></svg>;
  }
  if (name === "overview") {
    return <svg {...common}><path d="M4 11.5 12 5l8 6.5V20h-5v-5H9v5H4z" /></svg>;
  }
  if (name === "transactions") {
    return <svg {...common}><path d="M6 7h12M6 12h12M6 17h8" /><path d="m16 15 3 2-3 2" /></svg>;
  }
  if (name === "analytics") {
    return <svg {...common}><path d="M5 19V9M12 19V5M19 19v-7" /><path d="M3.5 19.5h17" /></svg>;
  }
  if (name === "more") {
    return (
      <svg {...common}>
        <circle cx="7" cy="7" r="1.5" /><circle cx="17" cy="7" r="1.5" />
        <circle cx="7" cy="17" r="1.5" /><circle cx="17" cy="17" r="1.5" />
      </svg>
    );
  }
  if (name === "budget") {
    return <svg {...common}><circle cx="12" cy="12" r="7.5" /><path d="M12 7.5v9M9.5 9.5h4a1.8 1.8 0 0 1 0 3.5h-3a1.8 1.8 0 0 0 0 3.5h4" /></svg>;
  }
  if (name === "recurring") {
    return <svg {...common}><path d="M18.5 8A7.5 7.5 0 0 0 6 6.5L4 9M5.5 16A7.5 7.5 0 0 0 18 17.5l2-2.5" /><path d="M4 5.5V9h3.5M20 18.5V15h-3.5" /></svg>;
  }
  if (name === "rates") {
    return <svg {...common}><path d="M5 8h13m-3-3 3 3-3 3M19 16H6m3 3-3-3 3-3" /></svg>;
  }
  if (name === "import") {
    return <svg {...common}><path d="M12 4v10m-4-3 4 4 4-4M5 19h14" /></svg>;
  }
  if (name === "shield") {
    return <svg {...common}><path d="M12 3.5 19 6v5.2c0 4.4-2.8 7.4-7 9.3-4.2-1.9-7-4.9-7-9.3V6z" /><path d="m9 12 2 2 4-4" /></svg>;
  }
  return <svg {...common}><path d="m9 5 7 7-7 7" /></svg>;
}
