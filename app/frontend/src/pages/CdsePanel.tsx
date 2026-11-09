// v1.7 dam CDSE (SCHEMA_CONTRACT §7 20): the platform admin assigns a dam's FIRST CDSE (not live, no CDSE yet; signed
// chain action with reason_code + rationale); later changes are admin proposals the CURRENT CDSE approves or rejects.
// The app never writes ref.dam: decisions go to the DA's load job through dam_cdse_outbox.
import { useState } from "react";
import { api } from "../api";
import { useApi } from "../hooks";
import { Loading, utc } from "../components/ui";
import { explain } from "../codes";

const BADGE: Record<string, string> = { pending: "warn", approved: "ok", rejected: "bad", loaded: "ok" };
const OUTBOX_STATUSES = ["pending", "loaded", "rejected"];   // §7 20: anything else is flagged, never "ok"

function ChangeRow({ ch, canDecide, policy, onDone }: { ch: any; canDecide: boolean; policy?: any; onDone: () => void }) {
  const [why, setWhy] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const act = async (what: "approve" | "reject") => {
    setErr(null);
    try { await api(`/api/dams/${ch.dam_id}/cdse/changes/${ch.change_id}/${what}`, { method: "POST", body: { rationale: why } }); onDone(); } catch (e: any) { setErr(e.message); }
  };
  const self = policy?.cdse_flag_require_second_approver && ch.proposed_by === policy?.me;
  return (
    <tr>
      <td>{ch.dam_id}</td><td>{ch.kind}</td><td title="previous = the dam's designated CDSE when proposed">{ch.previous_cdse_id || "—"} → <b>{ch.new_cdse_id}</b></td>
      <td>{ch.change_reason_code}<div className="muted">{ch.rationale}</div></td>
      <td>{ch.proposed_by}<div className="muted">{utc(ch.proposed_at)}</div></td>
      <td><span className={`badge ${BADGE[ch.status] || "muted"}`}>{ch.status}</span>
        {ch.outbox && <span className={`badge ${OUTBOX_STATUSES.includes(ch.outbox.status) ? BADGE[ch.outbox.status] : "bad"}`}>DA load {ch.outbox.status}{OUTBOX_STATUSES.includes(ch.outbox.status) ? "" : " (not a contract status)"}</span>}
        {ch.outbox?.reason_code && <div className="error">DA load refused: <b>{ch.outbox.reason_code}</b>{explain(ch.outbox.reason_code) ? ` — ${explain(ch.outbox.reason_code)}` : ""}<div className="muted">{ch.outbox.message}</div></div>}
        {ch.decided_by && <div className="muted">by {ch.decided_by} {utc(ch.decided_at)}: {ch.decision_rationale}</div>}</td>
      <td>{canDecide && ch.status === "pending" && (
        <div><input placeholder="rationale (required)" value={why} onChange={(e) => setWhy(e.target.value)} />
          {!self && <button onClick={() => act("approve")}>Approve</button>}
          <button className="secondary" onClick={() => act("reject")}>Reject</button>
          {err && <div className="error">{err}</div>}</div>)}</td>
    </tr>);
}

function Changes({ rows, canDecide, policy, onDone }: { rows: any[]; canDecide: boolean; policy?: any; onDone: () => void }) {
  if (!rows.length) return <div className="muted">No CDSE assignments or changes.</div>;
  return (
    <table><thead><tr><th>Dam</th><th>Kind</th><th>CDSE</th><th>Reason</th><th>Proposed</th><th>Status</th><th></th></tr></thead>
      <tbody>{rows.map((ch) => <ChangeRow key={ch.change_id} ch={ch} canDecide={canDecide} policy={policy} onDone={onDone} />)}</tbody></table>);
}

export function CdsePanel({ damId }: { damId?: string }) {
  const { data, error, loading, reload } = useApi<any>(damId ? `/api/dams/${damId}/cdse` : null);
  const me = useApi<any>("/api/me").data;
  const [f, setF] = useState<any>({});
  const [err, setErr] = useState<string | null>(null);
  if (!damId) return null;
  const set = (k: string) => (e: any) => setF({ ...f, [k]: e.target.value });
  const submit = async (path: string) => {
    setErr(null);
    try { await api(`/api/dams/${damId}/cdse/${path}`, { method: "POST", body: f }); setF({}); reload(); } catch (e: any) { setErr(e.message); }
  };
  const form = (label: string, path: string) => (
    <div className="form">
      <label>CDSE principal (e-mail) * <input value={f.cdse_id || ""} onChange={set("cdse_id")} /></label>
      <label>Reason code * <select value={f.reason_code || ""} onChange={set("reason_code")}>
        <option value="">— choose —</option>{(data.reason_codes || []).map((r: string) => <option key={r} value={r}>{r}</option>)}</select></label>
      <label>Rationale * <input value={f.rationale || ""} onChange={set("rationale")} /></label>
      <button onClick={() => submit(path)}>{label}</button>
    </div>);
  return (
    <div>
      <h4>Dam CDSE</h4>
      <Loading error={error} loading={loading} />
      {data && <div>Current CDSE: {data.current_cdse?.length ? <b>{data.current_cdse.join(", ")}</b> : <span className="badge bad" title={explain("no_cdse_in_reviewer_authority") || ""}>none — go-live blocked (no_cdse_in_reviewer_authority)</span>}
        {data.live && <span className="badge ok">live</span>}</div>}
      {data?.can_assign && form("Assign first CDSE (platform admin, signed)", "assign")}
      {data?.can_propose_change && form("Propose CDSE change (current CDSE approves)", "changes")}
      {data && !data.can_assign && !data.can_propose_change && !data.current_cdse?.length && <div className="muted">Only a platform admin can assign this dam's first CDSE.</div>}
      {err && <div className="error">{err}</div>}
      {data && <Changes rows={(data.changes || []).map((c: any) => ({ ...c, dam_id: damId }))} canDecide={data.can_decide} policy={{ ...data.approval_policy, me: me?.email }} onDone={reload} />}
    </div>);
}

export function CdseChangeQueue({ me }: { me?: any }) {
  const { data, reload } = useApi<any>("/api/cdse-changes/pending");
  if (!data) return null;
  if (!data.changes.length) return <div className="muted">No CDSE changes waiting for you.</div>;
  return <Changes rows={data.changes} canDecide policy={{ me: me?.email }} onDone={reload} />;
}
