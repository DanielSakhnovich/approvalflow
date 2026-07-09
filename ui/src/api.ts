// Typed fetch wrappers over the gateway's /api surface. Same-origin: the SPA is
// served by nginx, which also proxies /api/* to the services -- one entry point.

const BASE = "/api";
const TOKEN_STORAGE_KEY = "af_token";

// N1: module-level bearer-token holder. Set on login, read by apiFetch on
// every request, restored from localStorage on load so a page refresh
// doesn't force a re-login.
let token: string | null = localStorage.getItem(TOKEN_STORAGE_KEY);

// Fired whenever apiFetch sees a 401 (missing/invalid/expired token) so the
// app shell can drop back to the Login view regardless of which component
// triggered the request. App.tsx registers/clears this on mount/unmount.
type UnauthorizedListener = () => void;
let unauthorizedListener: UnauthorizedListener | null = null;

export function setUnauthorizedListener(listener: UnauthorizedListener | null): void {
  unauthorizedListener = listener;
}

export function getToken(): string | null {
  return token;
}

export function setToken(next: string | null): void {
  token = next;
  if (next) {
    localStorage.setItem(TOKEN_STORAGE_KEY, next);
  } else {
    localStorage.removeItem(TOKEN_STORAGE_KEY);
  }
}

async function apiFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  if (token) headers.Authorization = `Bearer ${token}`;
  const resp = await fetch(`${BASE}${path}`, { headers, ...init });
  if (resp.status === 401) {
    setToken(null);
    unauthorizedListener?.();
  }
  if (!resp.ok) {
    let detail = resp.statusText;
    try {
      const body = await resp.json();
      detail = body.detail ?? JSON.stringify(body);
    } catch {
      /* non-JSON error body */
    }
    throw new Error(`${resp.status}: ${detail}`);
  }
  return resp.json() as Promise<T>;
}

export interface InvoiceView {
  trackingId: string;
  clientRef: string | null;
  status: string;
  route: string | null;
  reasoning: string;
  decidedBy: string | null;
  submittedAt: string;
  updatedAt: string;
  trail?: TrailEntry[];
}

export interface TrailEntry {
  event_type: string;
  event_id: string;
  occurred_at: string;
  payload: Record<string, unknown>;
}

export interface QueueItem {
  invoiceId: string;
  status: string;
  recommendation: string;
  confidence: number | null;
  violations: string[];
  reasoning: string;
  usdCents: number;
  escalatedAt: string;
}

export interface Thresholds {
  ceiling_cents: number;
  trusted_ceiling_cents: number;
  min_confidence: number;
  saas_monthly_cap_cents: number;
  meals_per_attendee_cents: number;
}

export interface Compliance {
  autoApprovalsChecked: number;
  violations: unknown[];
}

export interface LoginResponse {
  access_token: string;
  token_type: string;
  role: string;
}

export const api = {
  login: async (username: string, password: string) => {
    const res = await apiFetch<LoginResponse>("/auth/login", {
      method: "POST",
      body: JSON.stringify({ username, password }),
    });
    setToken(res.access_token);
    return res;
  },
  logout: () => setToken(null),
  submit: (invoice: unknown) =>
    apiFetch<{ trackingId: string; correlationId: string; status: string }>(
      "/invoices",
      { method: "POST", body: JSON.stringify(invoice) },
    ),
  status: (id: string, trail = false) =>
    apiFetch<InvoiceView>(`/invoices/${id}${trail ? "?trail=true" : ""}`),
  resubmit: (id: string, invoice: unknown) =>
    apiFetch<{ trackingId: string; status: string }>(`/invoices/${id}`, {
      method: "PUT",
      body: JSON.stringify(invoice),
    }),
  dashboard: () => apiFetch<Record<string, number>>("/dashboard"),
  queue: () => apiFetch<{ items: QueueItem[] }>("/approvals/queue"),
  verdict: (id: string, verdict: string, approverId: string, comment: string) =>
    apiFetch<InvoiceView>(`/approvals/${id}/verdict`, {
      method: "POST",
      body: JSON.stringify({ verdict, approver_id: approverId, comment }),
    }),
  getThresholds: () => apiFetch<Thresholds>("/config/thresholds"),
  putThresholds: (patch: Partial<Thresholds>) =>
    apiFetch<Thresholds>("/config/thresholds", {
      method: "PUT",
      body: JSON.stringify(patch),
    }),
  compliance: () => apiFetch<Compliance>("/audit/ceiling-compliance"),
};

export const dollars = (cents: number) =>
  `$${(cents / 100).toLocaleString(undefined, { minimumFractionDigits: 2 })}`;
