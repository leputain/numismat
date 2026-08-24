export function protectedBackDestination(pathname: string): string | null {
  if (pathname === "/draft/compose") return "/draft";
  if (/^\/transactions\/[^/]+$/.test(pathname)) return "/transactions";
  if (pathname === "/budgets/new") return "/budgets";
  if (/^\/budgets\/[^/]+\/edit$/.test(pathname)) return pathname.replace(/\/edit$/, "");
  if (/^\/budgets\/[^/]+$/.test(pathname)) return "/budgets";
  if (pathname === "/recurring/new") return "/recurring";
  if (/^\/recurring\/[^/]+\/edit$/.test(pathname)) return pathname.replace(/\/edit$/, "");
  if (/^\/recurring\/[^/]+$/.test(pathname)) return "/recurring";
  if (/^\/imports\/[^/]+$/.test(pathname)) return "/imports";
  if (pathname === "/rates/new") return "/rates";
  if (/^\/rates\/[^/]+\/publish$/.test(pathname)) return pathname.replace(/\/publish$/, "");
  if (/^\/rates\/versions\/[^/]+$/.test(pathname)) return "/rates";
  if (/^\/rates\/[^/]+$/.test(pathname)) return "/rates";
  if (pathname === "/accounts" || pathname === "/categories" || pathname === "/settings") {
    return "/more";
  }
  return null;
}
