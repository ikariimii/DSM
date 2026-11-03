import { Link, useParams } from "react-router-dom";
import { useState } from "react";
import { api } from "../api";
import { useApi } from "../hooks";
import { Card, Loading, utc, Value } from "../components/ui";

export default function Dssmp() {
  const { damId } = useParams();
  const { data, error, loading, reload } = useApi<any>(`/api/dams/${damId}/dssmp`);
  if (!data) return <Loading error={error} loading={loading} />;
  return (
    <div>
      <h2>DSSMP — {data.site.name} ({damId})</h2>
      <div className="row"><Link to={`/dams/${damId}`}>← dam</Link></div>
      <div className="muted">Potential failure modes (PFMs) with the instruments and thresholds that monitor them.</div>
      {data.untied_instruments.length > 0 && (
        <div className="notice">Instruments not tied to any PFM (justify or link in the DSSMP): {data.untied_instruments.join(", ")}</div>
      )}
      {data.pfms.map((p: any) => (
        <Card key={p.pfm_id} title={`${p.pfm_id} — ${p.title}`} right={<span className="badge muted">category {p.category}</span>}>
          <p>{p.description}</p>
          <table className="kv"><tbody>
            <tr><td>Expected behaviour</td><td>{p.expected_behavior || "—"}</td></tr>
            <tr><td>Monitoring frequency</td><td>{p.monitoring_frequency || "—"}</td></tr>
          </tbody></table>
          {p.traceability_gaps.length > 0 && <div className="notice">Traceability gaps (missing instrument or no thresholds): {p.traceability_gaps.join(", ")}</div>}
          <table>
            <thead><tr><th>Instrument</th><th>Latest value</th><th>Thresholds</th></tr></thead>
            <tbody>
              {p.linked.map((l: any) => (
                <tr key={l.instrument_id}>
                  <td>{l.missing ? <span className="badge bad">{l.instrument_id} (missing)</span> : <Link to={`/instruments/${l.instrument_id}`}>{l.instrument_id}</Link>} <span className="muted">{l.instrument_type}</span></td>
                  <td>{l.latest ? <><Value value={l.latest.corrected_value ?? l.latest.eng_value} unit={l.latest.corrected_unit || l.latest.eng_unit}
                                             status={l.latest.display_status} calVersion={l.latest.cal_version} qa={l.latest.qa_flag} qaReason={l.latest.qa_reason} />
                    <div className="muted">{utc(l.latest.ts_utc)}</div></> : "—"}</td>
                  <td>{l.thresholds.map((t: any) => <div key={t.threshold_id}>L{t.level} {t.rule} {t.upper_limit ?? t.lower_limit ?? t.rate_limit_per_day} {t.unit}</div>)}
                    {l.thresholds.length === 0 && <span className="badge warn">none</span>}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <Reviews damId={damId!} pfm={p} canReview={data.can_review} onDone={reload} />
        </Card>
      ))}
    </div>
  );
}

function Reviews({ damId, pfm, canReview, onDone }: { damId: string; pfm: any; canReview: boolean; onDone: () => void }) {
  const [status, setStatus] = useState("no_change");
  const [notes, setNotes] = useState("");
  const [err, setErr] = useState<string | null>(null);
  return (
    <div>
      <h4>Periodic reviews</h4>
      {pfm.reviews.length === 0 && <div className="muted">No reviews recorded.</div>}
      {pfm.reviews.map((r: any) => <div key={r.id}>{utc(r.reviewed_at)} · {r.reviewed_by} · <b>{r.status}</b> {r.notes}</div>)}
      {canReview && (
        <div className="row">
          <select value={status} onChange={(e) => setStatus(e.target.value)}>
            <option value="no_change">no change</option><option value="update_required">update required</option><option value="updated">updated</option>
          </select>
          <input style={{ flex: 1 }} value={notes} onChange={(e) => setNotes(e.target.value)} placeholder="review notes" />
          <button onClick={async () => {
            setErr(null);
            try {
              await api(`/api/dams/${damId}/pfms/${pfm.pfm_id}/review`, { method: "POST", body: { status, notes } });
              setNotes("");
              onDone();
            } catch (e: any) { setErr(e.message); }
          }}>Record review</button>
          {err && <span className="error">{err}</span>}
        </div>
      )}
    </div>
  );
}
