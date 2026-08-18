import { AppRoutes } from "./routes";
import { AuthBoundary } from "../features/auth/auth-boundary";

export function App() {
  return (
    <AuthBoundary>
      <AppRoutes />
    </AuthBoundary>
  );
}
