import { useEffect } from "react";
import { Outlet, useLocation, useNavigate } from "react-router";

import { useAuth } from "../../features/auth/auth-context";
import { OfflineBanner, useIsOnline } from "./offline-banner";
import { PrimaryNavigation } from "./primary-navigation";

export function AppShell() {
  const { telegram } = useAuth();
  const location = useLocation();
  const navigate = useNavigate();
  const online = useIsOnline();
  const isTransactionDetail = location.pathname.startsWith("/transactions/");

  useEffect(() => {
    if (isTransactionDetail) {
      telegram.setBackButton(true, () => navigate("/transactions", { replace: true }));
    } else {
      telegram.setBackButton(false);
    }
    return () => telegram.setBackButton(false);
  }, [isTransactionDetail, navigate, telegram]);

  return (
    <div className="app-viewport flex min-h-dvh flex-col text-stone-100">
      <header className="border-b border-white/8 bg-[#0a1214]/90 px-[max(1.25rem,var(--tg-content-safe-area-left,0px))] pb-4 pt-[max(1rem,var(--tg-content-safe-area-top,0px))] backdrop-blur-xl">
        <div className="mx-auto flex max-w-4xl items-center justify-between gap-4">
          <div>
            <p className="font-serif text-xl tracking-tight text-stone-50">Numismat</p>
            <p className="text-[0.65rem] font-semibold uppercase tracking-[0.2em] text-stone-500">
              Private ledger
            </p>
          </div>
          <span className="rounded-full border border-emerald-300/15 bg-emerald-300/7 px-3 py-1.5 text-xs font-medium text-emerald-100/80">
            Локально
          </span>
        </div>
      </header>
      <OfflineBanner offline={!online} />
      <main className="mx-auto w-full max-w-4xl flex-1 px-[max(1.25rem,var(--tg-content-safe-area-left,0px))] py-6">
        <Outlet />
      </main>
      <PrimaryNavigation />
    </div>
  );
}
