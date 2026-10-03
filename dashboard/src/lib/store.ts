// The shared reactive store + typed FastAPI bridge client.
import { reactive } from "@arrow-js/core";
import type { StatsPayload } from "./types.js";

export const store = reactive<{ data: StatsPayload | null; error: string; billingBusy: boolean }>({
  data: null,
  error: "",
  billingBusy: false,
});

const jsonOrThrow = async (response: Response): Promise<unknown> => {
  if (response.status === 401) {
    window.location.href = "/_dashboard/login";
    throw new Error("session expired");
  }
  if (!response.ok) throw new Error(`API ${response.status}`);
  return response.json() as Promise<unknown>;
};

export async function refresh(): Promise<void> {
  try {
    const payload = (await jsonOrThrow(await fetch("/_dashboard/api/stats", { credentials: "same-origin" }))) as StatsPayload;
    store.data = payload;
    store.error = "";
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    if (message === "session expired") return;
    if (!store.data) store.error = message; // keep last-good data on transient errors
  }
}

export async function refreshBilling(): Promise<void> {
  if (store.billingBusy) return;
  store.billingBusy = true;
  try {
    await fetch("/_dashboard/api/billing/refresh", { method: "POST", credentials: "same-origin" });
    await refresh();
  } finally {
    store.billingBusy = false;
  }
}