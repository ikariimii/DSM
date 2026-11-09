// Thin fetch wrapper. In Databricks Apps the proxy adds X-Forwarded-Email; in demo mode the
// backend honours X-Demo-User (ignored outside demo mode) so the user switcher can test ACLs.
import { explain } from "./codes";

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
    const d = detail as any;
    // coded refusals (e.g. {code: "self_approval_forbidden", message}) show "code: message"
    super(typeof detail === "string" ? detail : d && typeof d.code === "string" && typeof d.message === "string"
      ? `${d.code}: ${d.message}${explain(d.code) ? ` — ${explain(d.code)}` : ""}` : JSON.stringify(detail));
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

export type DisplayStatus = "VERIFIED" | "UNVERIFIED_FORMULA" | "UNAPPROVED_CALIBRATION" | "UNAPPROVED_INSTALLATION" | "QC_BAD" | "MISSING"
  | "CORRECTION_UNVERIFIED" | "LINEAGE_INCOMPLETE" | "LINEAGE_INCONSISTENT" | "NO_ENGINE_FORMULA";

export interface ApprovalPolicy {
  cdse_flag_require_second_approver: boolean;
  provisional: boolean;
  cdse_may_apply_directly: boolean;
  cdse_may_self_approve: boolean;
  note: string;
}

export interface Me {
  email: string;
  known: boolean;
  is_admin: boolean;
  roles: Record<string, string>;
  dams: string[];
  demo_mode: boolean;
  can_onboard: boolean;
  // PROVISIONAL approval rule for live cdse_ack_required edits (config CDSE_FLAG_REQUIRE_SECOND_APPROVER)
  approval_policy?: ApprovalPolicy;
  demo_users?: { email: string; display_name: string }[];
}
