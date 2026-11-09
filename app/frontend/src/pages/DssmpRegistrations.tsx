// v1.7 DSSMP version registration: the onboarding wizard step and the CDSE approval panel (SCHEMA_CONTRACT §7 19).
// The app never writes ref.dssmp_version: an approved registration goes to the DA's load job through
// dssmp_registration_outbox; go-live needs the version LOADED there as approved and not superseded (fail closed).
import { useState } from "react";
import { api } from "../api";
import { useApi } from "../hooks";
import { Card, Loading, utc } from "../components/ui";

const REG_BADGE: Record<string, string> = { pending: "warn", approved: "ok", rejected: "bad" };
// contract spellings (loaded / rejected) and the DA load job's (applied / rejected_stale / rejected_invalid)
const OUTBOX_BADGE: Record<string, string> = { pending: "warn", loaded: "ok", applied: "ok", rejected: "bad", rejected_invalid: "bad", rejected_stale: "bad" };

function ProvisionalNote({ policy }: { policy: any }) {
  return <div className="muted"><span className="badge warn">provisional</span> {policy?.note ?? "Approval rule provisional: currently the dam CDSE may self-approve"} (DSSMP registrations follow the same rule).</div>;
}

function Status({ r }: { r: any }) {
  const o = r.outbox;
  return (
    <span>
      <span className={`badge ${REG_BADGE[r.status] || "muted"}`}>registration {r.status}</span>
      {o && <span className={`badge ${OUTBOX_BADGE[o.status] || "muted"}`}>DA load {o.status}{o.processed_at ? ` ${utc(o.processed_at)}` : ""}</span>}
      {r.status === "approved" && !r.ref && <span className="badge bad">not in ref.dssmp_version yet — go-live blocked</span>}
      {r.ref && <span className={`badge ${r.ref.status === "approved" && !r.ref.effective_to ? "ok" : "bad"}`}>ref {r.ref.status}{r.ref.effective_to ? ` until ${utc(r.ref.effective_to)}` : ""}</span>}
      {o && (o.reason_code || o.message) && <div className="error">Load job rejected it: <b>{o.reason_code || "no reason_code"}</b> {o.message}</div>}
    </span>
  );
}

export function RegistrationList({ damId, rows, canDecide, policy, onDone }: { damId: string; rows: any[]; canDecide: boolean; policy: any; onDone: () => void }) {
  const [why, setWhy] = useState<Record<string, string>>({});
  const [err, setErr] = useState<string | null>(null);
  const act = async (rid: string, what: "approve" | "reject") => {
    setErr(null);
    try {
      await api(`/api/dams/${damId}/dssmp-registrations/${rid}/${what}`, { method: "POST", body: { rationale: why[rid] || "" } });
      onDone();
    } catch (e: any) { setErr(e.message); }
  };
  if (!rows.length) return <div className="muted">No DSSMP registrations for {damId} yet.</div>;
  return (
    <div>
      {err && <div className="error">{err}</div>}
      <table>
        <thead><tr><th>Version</th><th>No.</th><th>Effective from (UTC)</th><th>Change reason / source</th><th>Document</th><th>Proposed</th><th>Status</th><th></th></tr></thead>
        <tbody>
          {rows.map((r) => {
            const self = policy?.cdse_flag_require_second_approver && r.proposed_by === policy?.me;
            return (
              <tr key={r.registration_id}>
                <td>{r.dssmp_version_id}</td><td>{r.version_no}</td><td>{utc(r.effective_from)}</td>
                <td>{r.change_reason}<div className="muted">{r.source_ref}</div></td>
                <td>{r.document_id
                  ? <a href={`/api/dams/${damId}/dssmp-documents/${r.document_id}`}>uploaded</a>
                  : <span>{r.document_uri}</span>}
                  <div className="muted" title="sha256">{r.document_sha256 ? `sha256 ${r.document_sha256.slice(0, 12)}…` : "no sha256"}</div></td>
                <td>{r.proposed_by}<div className="muted">{utc(r.proposed_at)}</div><div className="muted">{r.rationale}</div></td>
                <td><Status r={r} />{r.decided_by && <div className="muted">{r.status} by {r.decided_by} {utc(r.decided_at)}: {r.decision_rationale}</div>}</td>
                <td>{canDecide && r.status === "pending" && (
                  <div>
                    <input placeholder="rationale (required)" value={why[r.registration_id] || ""} onChange={(e) => setWhy({ ...why, [r.registration_id]: e.target.value })} />
                    {!self && <button onClick={() => act(r.registration_id, "approve")}>Approve</button>}
                    <button className="secondary" onClick={() => act(r.registration_id, "reject")}>{self ? "Withdraw (reject)" : "Reject"}</button>
                  </div>)}</td>
              </tr>);
          })}
        </tbody>
      </table>
    </div>
  );
}

function toB64(buf: ArrayBuffer): string {
  let s = ""; const b = new Uint8Array(buf);
  for (let i = 0; i < b.length; i += 0x8000) s += String.fromCharCode(...b.subarray(i, i + 0x8000));
  return btoa(s);
}

// Wizard step: register the dam's DSSMP version (dam_admin of the dam or platform admin).
export function DssmpRegistrationStep({ damId, disabled }: { damId?: string; disabled?: boolean }) {
  const { data, error, loading, reload } = useApi<any>(damId ? `/api/dams/${damId}/dssmp-registrations` : null);
  const me = useApi<any>("/api/me").data;
  const [f, setF] = useState<any>({ version_no: 1 });
  const [file, setFile] = useState<File | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  if (!damId) return <div className="muted">Enter the dam id in step 1 first.</div>;
  const set = (k: string) => (e: any) => setF({ ...f, [k]: e.target.value });
  const submit = async () => {
    setErr(null); setMsg(null);
    try {
      const b: any = { ...f, version_no: Number(f.version_no) };
      if (file) {
        const up: any = await api(`/api/dams/${damId}/dssmp-documents`, { method: "POST", body: { filename: file.name, content_type: file.type || "application/octet-stream", content_b64: toB64(await file.arrayBuffer()) } });
        b.document_id = up.document_id; delete b.document_uri; delete b.document_sha256;
        setMsg(`uploaded ${up.filename} (${up.size_bytes} bytes, sha256 ${up.sha256})`);
      }
      await api(`/api/dams/${damId}/dssmp-registrations`, { method: "POST", body: b });
      setF({ version_no: 1 }); setFile(null); reload();
    } catch (e: any) { setErr(e.message); }
  };
  return (
    <div>
      <p className="muted">Register the DSSMP version this dam's thresholds are set under. The dam's CDSE must approve it; the data layer then loads it into ref.dssmp_version. <b>Go-live is blocked</b> until it is loaded there as approved and not superseded.</p>
      <Loading error={error} loading={loading} />
      {data && <ProvisionalNote policy={data.approval_policy} />}
      {data && data.can_propose && !disabled && (
        <div className="form">
          <label>DSSMP version id * <input value={f.dssmp_version_id || ""} onChange={set("dssmp_version_id")} placeholder={`DSSMP-${damId}-2026`} /></label>
          <label>Version no. * (integer ≥ 1) <input type="number" min={1} step={1} value={f.version_no} onChange={set("version_no")} /></label>
          <label>Effective from * (UTC date) <input type="date" value={f.effective_from || ""} onChange={set("effective_from")} /></label>
          <label>Change reason * <input value={f.change_reason || ""} onChange={set("change_reason")} /></label>
          <label>Source ref * (DSSMR or IC recommendation id) <input value={f.source_ref || ""} onChange={set("source_ref")} /></label>
          <label>Document URI (or upload below) <input value={f.document_uri || ""} onChange={set("document_uri")} disabled={!!file} /></label>
          <label>Document sha256 * (64 hex, required with a URI) <input value={f.document_sha256 || ""} onChange={set("document_sha256")} disabled={!!file} /></label>
          <label>…or upload the document (max 20 MB; sha256 computed by the server) <input type="file" onChange={(e) => setFile(e.target.files?.[0] || null)} /></label>
          <label>Rationale * <input value={f.rationale || ""} onChange={set("rationale")} /></label>
          <button onClick={submit}>Submit for CDSE approval</button>
        </div>)}
      {data && !data.can_propose && <div className="muted">Only a dam_admin of {damId} or a platform admin can register a version (for a dam that is not live yet: a platform admin).</div>}
      {msg && <div className="muted">{msg}</div>}
      {err && <div className="error">{err}</div>}
      {data && <RegistrationList damId={damId} rows={data.registrations} canDecide={data.can_decide} policy={{ ...data.approval_policy, me: me?.email }} onDone={reload} />}
    </div>
  );
}

// CDSE approval panel: every pending registration of the dams the caller is the CDSE of.
export default function DssmpApprovals({ me }: { me?: any }) {
  const { data, error, loading, reload } = useApi<any>("/api/dssmp-registrations/pending");
  const rows: any[] = data?.registrations || [];
  const dams = Array.from(new Set(rows.map((r) => r.dam_id)));
  const policy = data && { ...data.approval_policy, me: me?.email };
  return (
    <Card title="DSSMP version approvals (CDSE)">
      <Loading error={error} loading={loading} />
      {data && <ProvisionalNote policy={data.approval_policy} />}
      {data && !rows.length && <div className="muted">Nothing waiting for your approval.</div>}
      {dams.map((d) => <div key={d}><h3>{d}</h3><RegistrationList damId={d} rows={rows.filter((r) => r.dam_id === d)} canDecide policy={policy} onDone={reload} /></div>)}
    </Card>
  );
}
