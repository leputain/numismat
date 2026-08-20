import { useEffect, useState } from "react";

export function useIsOnline(): boolean {
  const [online, setOnline] = useState(() =>
    typeof navigator === "undefined" ? true : navigator.onLine,
  );
  useEffect(() => {
    const handleOnline = () => setOnline(true);
    const handleOffline = () => setOnline(false);
    window.addEventListener("online", handleOnline);
    window.addEventListener("offline", handleOffline);
    return () => {
      window.removeEventListener("online", handleOnline);
      window.removeEventListener("offline", handleOffline);
    };
  }, []);
  return online;
}

export function OfflineBanner({ offline }: { readonly offline: boolean }) {
  if (!offline) {
    return null;
  }
  return (
    <div className="offline-banner" role="status">
      <span aria-hidden="true" />
      Нет сети. Можно просматривать загруженное, но изменения временно недоступны.
    </div>
  );
}
