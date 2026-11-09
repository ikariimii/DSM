import { Link, useParams } from "react-router-dom";
import { useState } from "react";
import { api } from "../api";
import { useApi } from "../hooks";
import { Card, Loading, utc } from "../components/ui";

// Live-dam edits of an alarm rule's "CDSE ack required" flag. dam_admin / admin may only PROPOSE; the dam's CDSE
// (ref.v_dam_reviewer_authority) approves, rejects or sets/clears directly. The server enforces all of this; the
// controls below are only shown to the roles that may use them. Every action needs a rationale and is chained.
// PROVISIONAL approval rule (data.approval_policy, config CDSE_FLAG_REQUIRE_SECOND_APPROVER): when a second approver
// is required, direct set/clear is hidden (can_apply false), the CDSE proposes too, and the CDSE's own proposals
// show only "Withdraw (reject)" (can_approve false).
export default function AlarmRules() {
  const { damId } = useParams();
  const { data, error, loading, reload } = useApi<any>(`/api/dams/${damId}/alarm-rules`);
  const [why, setWhy] = useState<Record<string, string>>({});
  const [err, setErr] = useState<string | null>(null);
  if (!data) return <Loading error={error} loading={loading} />;
  const text = (k: string) => (why[k] || "").trim();
  const run = async (k: string, path: string, body: any) => {
    setErr(null);
    try {
      await api(path, { method: "POST", body: { ...body, rationale: text(k) } });
      setWhy({ ...why, [k]: "" });
      reload();
    } catch (e: any) { setErr(e.message); }
  };
  const box = (k: string) => (
    <input type="text" size={28} placeholder="rationale (required)" value={why[k] || ""}
           onChange={(e) => setWhy({ ...why, [k]: e.target.value })} />
  );
  const base = `/api/dams/${damId}/alarm-rules`;
  const pol = data.approval_policy || {};
  const second = !!pol.cdse_flag_require_second_approver;
  return (
    <div>
      <h2>Alarm rules — {damId}</h2>
      <div className="row"><Link to={`/dams/${damId}`}>← dam</Link></div>
      <div className="muted">"CDSE ack required": alarms from the rule need this dam's CDSE to acknowledge them at any level (levels 2 and 3 always do). {data.is_cdse ? (second ? "You are this dam's CDSE: you may propose changes and approve or reject other people's proposals; your own proposals can only be withdrawn." : "You are this dam's CDSE: you may set or clear the flag and decide proposals.") : data.can_propose ? "You may propose a change; the dam's CDSE approves or rejects it." : "Read only."} Clearing a flag never weakens an alarm that is already open. Each change creates a new rule version.</div>
      <div className="muted"><span className="badge warn">provisional</span> {pol.note || "Approval rule provisional: currently the dam CDSE may self-approve"}.</div>
      {err && <div className="error">{err}</div>}
      <Card title="Rules">
        <table><thead><tr><th>Rule</th><th>Version</th><th>Instrument</th><th>Level</th><th>Limit</th><th>CDSE ack required</th><th></th></tr></thead><tbody>
          {data.rules.map((r: any) => (
            <tr key={r.threshold_id}>
              <td>{r.threshold_id}</td><td>v{r.threshold_version}</td><td>{r.instrument_id}</td><td>L{r.level}</td>
              <td>{r.rule_label} {r.value} {r.unit}</td>
              <td>{r.cdse_ack_required ? <b>yes</b> : "no"}{r.pending.length > 0 && <span className="badge warn"> {r.pending.length} pending</span>}</td>
              <td>
                {(data.can_propose || data.can_apply) && box(r.threshold_id)}
                {data.can_propose && (!data.is_cdse || second) && (
                  <button disabled={!text(r.threshold_id)} onClick={() => run(r.threshold_id, `${base}/${r.threshold_id}/cdse-flag/proposals`, { cdse_ack_required: !r.cdse_ack_required })}>
                    Propose {r.cdse_ack_required ? "clear" : "set"}</button>
                )}
                {data.can_apply && (
                  <button disabled={!text(r.threshold_id)} onClick={() => run(r.threshold_id, `${base}/${r.threshold_id}/cdse-flag`, { cdse_ack_required: !r.cdse_ack_required })}>
                    {r.cdse_ack_required ? "Clear" : "Set"} (CDSE)</button>
                )}
              </td>
            </tr>
          ))}
        </tbody></table>
      </Card>
      <Card title={`Pending proposals (${data.pending.length})`}>
        {data.pending.length === 0 && <div className="muted">none</div>}
        {data.pending.length > 0 && (
          <table><thead><tr><th>Rule</th><th>Change</th><th>Proposed by</th><th>When (UTC)</th><th>Rationale</th><th></th></tr></thead><tbody>
            {data.pending.map((p: any) => (
              <tr key={p.proposal_id}>
                <td>{p.threshold_id} (v{p.base_threshold_version})</td><td>{p.base_flag ? "yes" : "no"} → <b>{p.new_flag ? "yes" : "no"}</b></td>
                <td>{p.proposed_by}</td><td>{utc(p.proposed_at)}</td><td>{p.rationale}</td>
                <td>{p.can_decide && (<>
                  {box(p.proposal_id)}
                  {p.can_approve && <button disabled={!text(p.proposal_id)} onClick={() => run(p.proposal_id, `${base}/proposals/${p.proposal_id}/approve`, {})}>Approve</button>}
                  {p.can_reject && <button className="secondary" disabled={!text(p.proposal_id)} onClick={() => run(p.proposal_id, `${base}/proposals/${p.proposal_id}/reject`, {})}>{p.own && second ? "Withdraw (reject)" : "Reject"}</button>}
                  {p.own && second && <span className="muted"> your proposal: another CDSE must approve</span>}
                </>)}</td>
              </tr>
            ))}
          </tbody></table>
        )}
      </Card>
      {data.history.length > 0 && (
        <Card title="Decided">
          <table><thead><tr><th>Rule</th><th>Change</th><th>Status</th><th>Proposed by</th><th>Decided by</th><th>When (UTC)</th></tr></thead><tbody>
            {data.history.map((p: any) => (
              <tr key={p.proposal_id}><td>{p.threshold_id}</td><td>{p.base_flag ? "yes" : "no"} → {p.new_flag ? "yes" : "no"}</td>
                <td>{p.status}{p.new_threshold_version ? ` (v${p.new_threshold_version})` : ""}</td><td>{p.proposed_by}</td><td>{p.decided_by}</td><td>{utc(p.decided_at)}</td></tr>
            ))}
          </tbody></table>
        </Card>
      )}
    </div>
  );
}
