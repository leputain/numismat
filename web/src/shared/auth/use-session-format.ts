import { useAuth } from "../../features/auth/auth-context";

export function useSessionFormat(): {
  readonly locale: string;
  readonly baseCurrency: string;
  readonly timeZone: string;
} {
  const { state } = useAuth();
  if (state.status !== "authenticated") {
    return { locale: "ru-RU", baseCurrency: "RUB", timeZone: "UTC" };
  }
  return {
    locale: state.session.locale,
    baseCurrency: state.session.baseCurrency,
    timeZone: state.session.timezone,
  };
}
