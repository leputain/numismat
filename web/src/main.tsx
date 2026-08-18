import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { App } from "./app/app";
import { AppErrorBoundary } from "./app/error-boundary";
import { AppProviders } from "./app/providers";
import "./styles.css";

const rootElement = document.getElementById("root");

if (!(rootElement instanceof HTMLElement)) {
  throw new Error("Application root is unavailable");
}

createRoot(rootElement).render(
  <StrictMode>
    <AppErrorBoundary>
      <AppProviders>
        <App />
      </AppProviders>
    </AppErrorBoundary>
  </StrictMode>,
);
