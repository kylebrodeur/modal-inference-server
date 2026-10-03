// Dashboard entrypoint: mount the Arrow view + drive the 15s refresh.
import "./lib/analytics.js";
import { store, refresh } from "./lib/store.js";
import { mountDashboard } from "./views/Dashboard.js";

const root = document.querySelector<HTMLElement>("#app");
if (!root) throw new Error("Missing #app root");

const dispose = mountDashboard(root);

// Static-shell health pill: bound through the same store so it stays live.
const pill = document.querySelector("#health");
import { watch } from "@arrow-js/core";
import { statusClass } from "./lib/analytics.js";
watch(() => {
  void store.data;
  const health = store.data?.deployment.health ?? "";
  if (pill) {
    pill.textContent = store.data ? health : "—";
    pill.className = "pill " + statusClass(health);
  }
});

const shellRefresh = document.querySelector<HTMLButtonElement>("#refresh");
shellRefresh?.addEventListener("click", () => void refresh());

void refresh();
setInterval(() => void refresh(), 15_000);

window.addEventListener("pagehide", () => dispose(), { once: true });