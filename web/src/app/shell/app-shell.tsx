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
    window.scrollTo({ top: 0, left: 0, behavior: "auto" });
  }, [location.pathname]);

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
      <header className="shell-header">
        <div className="shell-header__inner">
          <div className="shell-brand">
            <span aria-hidden="true" className="shell-brand__mark">N</span>
            <div>
              <p className="shell-brand__name">Numismat</p>
              <p className="shell-brand__caption">Личные финансы</p>
            </div>
          </div>
          <span className="shell-status">
            <i aria-hidden="true" />
            Личный контур
          </span>
        </div>
      </header>
      <OfflineBanner offline={!online} />
      <main className="shell-main">
        <Outlet />
      </main>
      <PrimaryNavigation />
    </div>
  );
}
