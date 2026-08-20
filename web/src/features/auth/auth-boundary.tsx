import type { PropsWithChildren } from "react";

import { AuthErrorState } from "../../app/states/auth-error-state";
import { LoadingState } from "../../app/states/loading-state";
import { useAuth } from "./auth-context";

export function AuthBoundary({ children }: PropsWithChildren) {
  const auth = useAuth();
  switch (auth.state.status) {
    case "authenticated":
      return children;
    case "booting":
      return <LoadingState />;
    case "authenticating_telegram":
      return <LoadingState message="Подтверждаем доступ через Telegram…" />;
    case "auth_result_unknown":
      return <LoadingState message="Проверяем результат входа…" />;
    case "sdk_unavailable":
      return (
        <AuthErrorState
          actionLabel="Закрыть"
          description="Запустите приложение из официального интерфейса Telegram."
          onAction={auth.closeMiniApp}
          title="Telegram Mini App недоступно"
        />
      );
    case "access_denied":
      return (
        <AuthErrorState
          actionLabel="Закрыть"
          description="Этот Telegram-профиль не входит в список разрешённых пользователей."
          onAction={auth.closeMiniApp}
          title="Доступ запрещён"
        />
      );
    case "deployment_error":
      return (
        <AuthErrorState
          actionLabel="Закрыть"
          description="Источник Mini App не совпадает с настройкой сервера. Обратитесь к администратору."
          onAction={auth.closeMiniApp}
          title="Ошибка конфигурации"
        />
      );
    case "protocol_error":
      return (
        <AuthErrorState
          actionLabel="Закрыть"
          description="Сервер вернул неожиданный ответ. Детали ответа не сохранялись."
          onAction={auth.closeMiniApp}
          title="Сервис временно недоступен"
        />
      );
    case "reopen_required":
    case "signed_out":
      return (
        <AuthErrorState
          actionLabel="Закрыть"
          description="Закройте Mini App и откройте его снова, чтобы получить новые данные входа."
          onAction={auth.closeMiniApp}
          title="Требуется новый вход"
        />
      );
  }
}
