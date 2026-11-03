// Thin fetch wrapper. In Databricks Apps the proxy adds X-Forwarded-Email; in demo mode the
// backend honours X-Demo-User (ignored outside demo mode) so the user switcher can test ACLs.
const DEMO_KEY = "dam-demo-user";

export function demoUser(): string | null {
  return localStorage.getItem(DEMO_KEY);
}
export function setDemoUser(email: string | null) {
  if (email) localStorage.setItem(DEMO_KEY, email);
  else localStorage.removeItem(DEMO_KEY);
}

export class ApiError extends Error {
  constructor(public status: number, public detail: unknown) {
    super(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
}

export async function api<T = any>(path: string, opts: { method?: string; body?: unknown } = {}): Promise<T> {
  const headers: Record<string, string> = { Accept: "application/json" };
  const du = demoUser();
  if (du) headers["X-Demo-User"] = du;
  if (opts.body !== undefined) headers["Content-Type"] = "application/json";
  const r = await fetch(path, {
    method: opts.method || "GET",
    headers,
    body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
  });
  const text = await r.text();
  const data = text ? JSON.parse(text) : null;
  if (!r.ok) throw new ApiError(r.status, data?.detail ?? data ?? r.statusText);
  return data as T;
}

export type DisplayStatus = "VERIFIED" | "UNVERIFIED_FORMULA" | "UNAPPROVED_CALIBRATION" | "QC_BAD" | "MISSING"
  | "CORRECTION_UNVERIFIED" | "LINEAGE_INCOMPLETE" | "LINEAGE_INCONSISTENT" | "NO_ENGINE_FORMULA";

export interface Me {
  email: string;
  known: boolean;
  is_admin: boolean;
  roles: Record<string, string>;
  dams: string[];
  demo_mode: boolean;
  can_onboard: boolean;
  demo_users?: { email: string; display_name: string }[];
}
