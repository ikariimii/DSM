import { Link, useParams } from "react-router-dom";
import { useState } from "react";
import { api, type Me } from "../api";
import { useApi } from "../hooks";
import { Card, Loading, utc } from "../components/ui";

export default function Reports({ me }: { me: Me }) {
  const { damId } = useParams();
  const { data, error, reload } = useApi<{ reports: any[] }>(`/api/dams/${damId}/reports`);
  const [sel, setSel] = useState<string | null>(null);
  const { data: rep } = useApi<any>(sel ? `/api/dams/${damId}/reports/${sel}` : null);
  const year = new Date().getUTCFullYear();
  const [start, setStart] = useState(`${year}-01-01`);
  const [end, setEnd] = useState(new Date().toISOString().slice(0, 10));
  const [err, setErr] = useState<string | null>(null);
  const role = me.is_admin ? "dam_admin" : me.roles[damId || ""];
  const canDraft = ["engineer", "dso", "dam_admin"].includes(role || "");
  return (
    <div>
      <h2>DSSMR draft reports — {damId}</h2>
      <div className="row"><Link to={`/dams/${damId}`}>← dam</Link></div>
      <div className="notice">Drafts are generated from verified values only; withheld values are listed as such. A draft is not a signed report.</div>
      {canDraft && (
        <Card title="New draft">
          <div className="row">
            period (UTC) <input type="date" value={start} onChange={(e) => setStart(e.target.value)} /> →
            <input type="date" value={end} onChange={(e) => setEnd(e.target.value)} />
            <button onClick={async () => {
              setErr(null);
              try {
                const r = await api(`/api/dams/${damId}/reports`, { method: "POST", body: { period_start: start, period_end: end } });
                reload();
                setSel(r.report_id);
              } catch (e: any) { setErr(e.message); }
            }}>Generate draft</button>
          </div>
          {err && <div className="error">{err}</div>}
        </Card>
      )}
      <Loading error={error} />
      <Card title="Drafts">
        <table><thead><tr><th>Created (UTC)</th><th>By</th><th>Period</th><th>Status</th><th></th></tr></thead><tbody>
          {data?.reports.map((r) => (
            <tr key={r.report_id}><td>{utc(r.created_at)}</td><td>{r.created_by}</td><td>{utc(r.period_start)} → {utc(r.period_end)}</td>
              <td>{r.status}</td><td><button className="secondary" onClick={() => setSel(r.report_id)}>view</button></td></tr>
          ))}
        </tbody></table>
        {data && data.reports.length === 0 && <div className="muted">No drafts yet.</div>}
      </Card>
      {rep && (
        <Card title={`Draft ${rep.report_id}`} right={<button className="secondary" onClick={() => {
          const b = new Blob([rep.content_md], { type: "text/markdown" });
          const a = document.createElement("a");
          a.href = URL.createObjectURL(b);
          a.download = `DSSMR_${damId}_${rep.period_end.slice(0, 10)}_DRAFT.md`;
          a.click();
        }}>Download .md</button>}>
          <pre className="md">{rep.content_md}</pre>
        </Card>
      )}
    </div>
  );
}
