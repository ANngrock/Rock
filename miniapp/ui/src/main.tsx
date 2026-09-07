import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App.tsx";
import "./glass.css";

const tg = (window as unknown as { Telegram?: { WebApp?: any } }).Telegram?.WebApp;
tg?.ready?.();
tg?.setHeaderColor?.("#0b0d12");
tg?.setBackgroundColor?.("#0b0d12");
tg?.expand?.();

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
