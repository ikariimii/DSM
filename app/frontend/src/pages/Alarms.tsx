import { Link, useSearchParams } from "react-router-dom";
import { Fragment, useState } from "react";
import { api } from "../api";
import { useApi } from "../hooks";
import { Card, LevelBadge, Loading, QcBadge, StatusBadge, fmt, utc } from "../components/ui";

export default function Alarms() {
  const [sp, setSp] = useSearchParams();
  const dam = sp.get("dam_id") || "";
  const state = sp.get("state") || "";
  const qs = new URLSearchParams();
  if (dam) qs.set("dam_id", dam);
  if (state) qs.set("state", state);
  const { data, error, loading, reload } = useApi<{ alarms: any[] }>(`/api/alarms?${qs}`);
  const [open, setOpen] = useState<string | null>(null);
  const set = (k: string, v: string) => {
    const n = new URLSearchParams(sp);
    if (v) n.set(k, v); else n.delete(k);
    setSp(n);
  };
  return (
    <div>
      <h2>Alarms</h2>
      <div className="row">
        <input placeholder="dam id" value={dam} onChange={(e) => set("dam_id", e.target.value.toUpperCase())} />
        <select value={state} onChange={(e) => set("state", e.target.value)}>
          <option value="">all states</option><option>open</option><option>acknowledged</option><option>closed</option>
        </select>
      </div>
      <Loading error={error} loading={loading} />
      <Card>
        <table>
          <thead><tr><th>Level</th><th>Dam / instrument</th><th>Trigger</th><th>Lineage</th><th>Raised (UTC)</th><th>State</th><th></th></tr></thead>
          <tbody>
            {data?.alarms.map((a) => (
              <Fragment key={a.event_id}>
                <tr>
                  <td><LevelBadge level={a.level} name={a.level_name} /></td>
                  <td><Link to={`/dams/${a.dam_id}`}>{a.dam_id}</Link> / <Link to={`/instruments/${a.instrument_id}`}>{a.instrument_id}</Link></td>
                  <td>
                    {a.message}
                    <div>value {a.value === null ? <span className="withheld">withheld</span> : <b>{fmt(a.value)} {a.unit}</b>} · limit {a.limit ?? "—"} {a.unit}</div>
                  </td>
                  <td>cal {a.calib_id || "—"} v{a.cal_version ?? "—"} · {a.formula_id || "—"} v{a.formula_version || "—"}<div><QcBadge flag={a.qa_flag} /> <StatusBadge status={a.display_status} /></div></td>
                  <td>{utc(a.event_ts)}{a.cleared_ts && <div className="muted">cleared {utc(a.cleared_ts)}</div>}</td>
                  <td>{a.state}{a.acknowledged_by && <div className="muted">ack {a.acknowledged_by}</div>}{a.closed_by && <div className="muted">closed {a.closed_by}</div>}</td>
                  <td><button className="secondary" onClick={() => setOpen(open === a.event_id ? null : a.event_id)}>{open === a.event_id ? "hide" : "details"}</button></td>
                </tr>
                {open === a.event_id && (
                  <tr><td colSpan={7}><AlarmDetail id={a.event_id} onChange={reload} /></td></tr>
                )}
              </Fragment>
            ))}
          </tbody>
        </table>
        {data && data.alarms.length === 0 && <div className="muted">No alarms.</div>}
      </Card>
    </div>
  );
}

function AlarmDetail({ id, onChange }: { id: string; onChange: () => void }) {
  const { data, error, reload } = useApi<any>(`/api/alarms/${id}`);
  const [text, setText] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const act = async (path: string, body: unknown) => {
    setErr(null);
    try {
      await api(`/api/alarms/${id}/${path}`, { method: "POST", body });
      setText("");
      reload();
      onChange();
    } catch (e: any) {
      setErr(e.message);
    }
  };
  if (!data) return <Loading error={error} />;
  return (
    <div>
      {data.pfm_ids?.length > 0 && <div>Linked PFMs: {data.pfm_ids.join(", ")}</div>}
      {data.response_action && <div>Required action: <b>{data.response_action}</b></div>}
      <h4>Comments</h4>
      {(data.comments || []).map((c: any) => <div key={c.id}><span className="muted">{utc(c.created_at)} {c.author}:</span> {c.body}</div>)}
      {(data.comments || []).length === 0 && <div className="muted">none</div>}
      <textarea rows={2} value={text} onChange={(e) => setText(e.target.value)} placeholder="comment / ack note / close reason (≥3 chars)" />
      {err && <div className="error">{err}</div>}
      <div className="row">
        <button disabled={!text.trim()} onClick={() => act("comment", { body: text })}>Comment</button>
        <button disabled={!data.can_ack} title={data.can_ack ? "" : "engineer role or higher, open alarms only"} onClick={() => act("ack", { comment: text || null })}>Acknowledge</button>
        <button disabled={!data.can_close || text.trim().length < 3} title={data.can_close ? "" : "DSO role or higher, acknowledged alarms only"} onClick={() => act("close", { reason: text })}>Close</button>
      </div>
    </div>
  );
}
