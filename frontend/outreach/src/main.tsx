import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import App from "./App";
import { AppProvider } from "./app/AppContext";
import { BASENAME, pageBoot } from "./boot";
import "./index.css";

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <BrowserRouter basename={BASENAME} future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
      <AppProvider>
        <App />
      </AppProvider>
    </BrowserRouter>
  </React.StrictMode>,
);

// The service worker is served by Frappe at /outreach/sw.js so it can control /outreach/ (not in dev).
if (import.meta.env.PROD && "serviceWorker" in navigator) {
  const swUrl = pageBoot()?.sw_url ?? "/outreach/sw.js";
  window.addEventListener("load", () => {
    navigator.serviceWorker.register(swUrl, { scope: "/outreach/" }).catch((e: unknown) => {
      console.warn("Service worker registration failed", e);
    });
  });
  // Ask the browser not to evict the encrypted records under storage pressure.
  void navigator.storage?.persist?.();
}
