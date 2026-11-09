import { Link, useParams } from "react-router-dom";
import { useMemo, useState } from "react";
import type { Me } from "../api";
import { useApi } from "../hooks";
import { Card, LevelBadge, Loading, StatusBadge, utc, Value } from "../components/ui";

export default function Dam({ me }: { me: Me }) {
  const { damId } = useParams();
  const { data, error, loading } = useApi<any>(`/api/dams/${damId}`);
  const [type, setType] = useState("");
  const [q, setQ] = useState("");
  const insts = useMemo(
    () => (data?.instruments || []).filter((i: any) => (!type || i.instrument_type === type) &&
      (!q || i.instrument_id.toLowerCase().includes(q.toLowerCase()))),
    [data, type, q]
  );
  if (!data) return <Loading error={error} loading={loading} />;
  const s = data.site;
  const types = Array.from(new Set((data.instruments || []).map((i: any) => i.instrument_type))) as string[];
  return (
    <div>
      <h2>{s.name} ({s.dam_id}) <LevelBadge level={data.summary.max_active_level} name={data.summary.max_active_level_name} /></h2>
      <div className="row">
        <span className="muted">{s.dam_type} · hazard {s.hazard_class} · owner {s.owner} · datum {s.datum} · your role: <b>{me.is_admin ? "admin" : data.my_role}</b></span>
        <Link to={`/dams/${damId}/dssmp`}>DSSMP / PFMs</Link>
        <Link to={`/dams/${damId}/reports`}>DSSMR reports</Link>
        <Link to={`/alarms?dam_id=${damId}`}>Alarms</Link>
        <Link to={`/dams/${damId}/alarm-rules`}>Alarm rules</Link>
      </div>
      <Card title="Structures">
        <table><thead><tr><th>ID</th><th>Name</th><th>Type</th><th>Stations (m)</th></tr></thead><tbody>
          {data.structures.map((x: any) => (
            <tr key={x.structure_id}><td>{x.structure_id}</td><td>{x.name}</td><td>{x.structure_type}</td>
              <td>{x.station_from_m ?? "—"} – {x.station_to_m ?? "—"}</td></tr>
          ))}
        </tbody></table>
      </Card>
      <Card title={`Instrument registry (${insts.length})`} right={
        <span className="row">
          <select value={type} onChange={(e) => setType(e.target.value)}>
            <option value="">all types</option>
            {types.map((t) => <option key={t}>{t}</option>)}
          </select>
          <input placeholder="search id" value={q} onChange={(e) => setQ(e.target.value)} />
        </span>}>
        <table>
          <thead><tr><th>Instrument</th><th>Type</th><th>Structure / station</th><th>Formula</th><th>Calibration</th><th>Latest (UTC)</th><th>Latest value</th></tr></thead>
          <tbody>
            {insts.map((i: any) => {
              const l = i.latest;
              return (
                <tr key={i.instrument_id}>
                  <td><Link to={`/instruments/${i.instrument_id}`}>{i.instrument_id}</Link><div className="muted">{i.status}</div></td>
                  <td>{i.instrument_type}<div className="muted">{i.subtype}</div></td>
                  <td>{i.structure_id}<div className="muted">sta {i.station_m ?? "—"} m, off {i.offset_m ?? "—"} m</div></td>
                  <td>{i.formula_id} v{i.formula_version} {i.formula_verified ? <span className="badge ok">verified</span> : <span className="badge warn">unverified</span>}</td>
                  <td>{i.current_calibration ? <>v{i.current_calibration.cal_version} {i.current_calibration.approved_by ? <span className="badge ok">approved</span> : <span className="badge warn">not approved</span>}</> : <span className="badge warn">none in force</span>}</td>
                  <td>{l ? utc(l.ts_utc) : <span className="muted">no data</span>}</td>
                  <td>{l ? <Value value={l.corrected_value ?? l.eng_value} unit={l.corrected_unit || l.eng_unit} status={l.display_status}
                                  calVersion={l.cal_version} qa={l.qa_flag} qaReason={l.qa_reason} /> : i.instrument_type === "inclinometer" ? <span className="muted">profile — open detail</span> : "—"}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </Card>
      <Card title="Active alarms">
        {data.alarms.filter((a: any) => a.state !== "closed").length === 0 && <div className="muted">None</div>}
        <table><tbody>
          {data.alarms.filter((a: any) => a.state !== "closed").map((a: any) => (
            <tr key={a.event_id}><td><LevelBadge level={a.level} name={a.level_name} /></td><td>{a.instrument_id}</td>
              <td>{a.display_status === "VERIFIED" ? a.message : <>{a.instrument_id} {a.rule_label || a.rule} L{a.level} <StatusBadge status={a.display_status} /></>}</td>
              <td>{a.state}</td><td>{utc(a.event_ts)}</td></tr>
          ))}
        </tbody></table>
      </Card>
    </div>
  );
}
