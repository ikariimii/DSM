import { Link } from "react-router-dom";
import { useApi } from "../hooks";
import { Card, Counts, LevelBadge, Loading } from "../components/ui";

export default function Portfolio() {
  const { data, error, loading } = useApi<{ dams: any[] }>("/api/portfolio");
  return (
    <div>
      <h2>Portfolio</h2>
      <Loading error={error} loading={loading} />
      {data && data.dams.length === 0 && <div className="notice">You do not have access to any dam.</div>}
      <div className="grid">
        {data?.dams.map((d) => (
          <Card key={d.dam_id} title={<Link to={`/dams/${d.dam_id}`}>{d.name} ({d.dam_id})</Link>}
                right={<LevelBadge level={d.max_active_level} name={d.max_active_level_name} />}>
            <div className="muted">{d.dam_type} · hazard {d.hazard_class} · {d.state} · NID {d.nid_id}</div>
            <table className="kv"><tbody>
              <tr><td>Crest / normal pool</td><td>{d.crest_elev_m ?? "—"} m / {d.normal_pool_m ?? "—"} m ({d.datum})</td></tr>
              <tr><td>Instruments reporting</td><td>{d.n_reporting} of {d.n_instruments}</td></tr>
              <tr><td>Latest display status</td><td><Counts obj={d.latest_status_counts} /></td></tr>
              <tr><td>Latest QC flags</td><td><Counts obj={d.latest_qa_counts} /></td></tr>
              <tr><td>Alarms</td><td>{d.alarms_open} open · {d.alarms_acknowledged} acknowledged</td></tr>
            </tbody></table>
            {d.n_unverified > 0 && (
              <div className="notice">{d.n_unverified} latest value(s) withheld (unverified formula, unapproved calibration or QC).</div>
            )}
          </Card>
        ))}
      </div>
    </div>
  );
}
