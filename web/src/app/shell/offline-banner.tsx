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
    <div className="border-b border-amber-200/15 bg-amber-100/8 px-5 py-2 text-center text-xs text-amber-100" role="status">
      Нет сети. Доступны только уже загруженные данные; изменения временно отключены.
    </div>
  );
}
