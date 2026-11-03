import { Link, useParams } from "react-router-dom";
import { useMemo, useState } from "react";
import {
  CartesianGrid, ComposedChart, Legend, Line, LineChart, ReferenceLine, ResponsiveContainer, Scatter, Tooltip, XAxis, YAxis,
} from "recharts";
import { useApi } from "../hooks";
import { Card, Counts, Loading, QcBadge, StatusBadge, fmt, utc } from "../components/ui";

export default function InstrumentDetail() {
  const { instrumentId } = useParams();
  const { data, error, loading } = useApi<any>(`/api/instruments/${instrumentId}`);
  if (!data) return <Loading error={error} loading={loading} />;
  const i = data.instrument;
  return (
    <div>
      <h2>{i.instrument_id} <span className="muted">({i.instrument_type}{i.subtype ? ` / ${i.subtype}` : ""})</span></h2>
      <div className="row"><Link to={`/dams/${i.dam_id}`}>← {i.dam_id}</Link></div>
      <div className="grid">
        <Card title="Registry">
          <table className="kv"><tbody>
            <tr><td>Structure</td><td>{i.structure_id}</td></tr>
            <tr><td>Station / offset</td><td>{i.station_m ?? "—"} m / {i.offset_m ?? "—"} m</td></tr>
            <tr><td>Tip / top elevation</td><td>{i.tip_elev_m ?? "—"} m / {i.top_elev_m ?? "—"} m</td></tr>
            <tr><td>Output</td><td>{i.output_name} [{i.eng_unit}]</td></tr>
            <tr><td>Installed</td><td>{i.install_date || "—"} · {i.status}</td></tr>
          </tbody></table>
        </Card>
        <Card title="Formula">
          {data.formula ? (
            <table className="kv"><tbody>
              <tr><td>ID / version</td><td>{data.formula.formula_id} v{data.formula.formula_version}</td></tr>
              <tr><td>Status</td><td>{data.formula.verified ? <span className="badge ok">verified</span> : <span className="badge warn">UNVERIFIED — values withheld</span>}</td></tr>
              <tr><td>Definition</td><td>{data.formula.description}</td></tr>
              <tr><td>Output unit</td><td>{data.formula.unit}</td></tr>
            </tbody></table>
          ) : <div className="notice">Formula {i.formula_id} v{i.formula_version} is not registered — values withheld.</div>}
        </Card>
      </div>
      <Card title="Calibration history">
        <table><thead><tr><th>Calib</th><th>Version</th><th>Effective (UTC)</th><th>Method</th><th>Parameters</th><th>Approval</th><th>Certificate</th></tr></thead><tbody>
          {data.calibrations.map((c: any) => (
            <tr key={c.calib_id}>
              <td>{c.calib_id}</td><td>v{c.cal_version} {c.is_current && <span className="badge ok">current</span>}</td>
              <td>{utc(c.effective_from)} → {c.effective_to ? utc(c.effective_to) : "now"}</td>
              <td>{c.method || "—"}</td>
              <td><code>{Object.entries(c.params || {}).map(([k, v]) => `${k}=${v}`).join(", ")}</code></td>
              <td>{c.approved_by ? <span className="badge ok">{c.approved_by} {utc(c.approved_at)}</span> : <span className="badge warn">NOT APPROVED — values withheld</span>}</td>
              <td>{c.certificate || "—"}</td>
            </tr>
          ))}
        </tbody></table>
      </Card>
      <Card title="Thresholds (current)">
        {data.thresholds.length === 0 && <div className="muted">No thresholds defined.</div>}
        <table><tbody>
          {data.thresholds.map((t: any) => (
            <tr key={t.threshold_id}>
              <td>L{t.level}</td><td>{t.rule}</td>
              <td>{t.upper_limit ?? t.lower_limit ?? t.rate_limit_per_day} {t.unit}{t.rule === "rate_above" ? "/day" : ""}</td>
              <td>{(t.pfm_ids || []).join(", ")}</td><td>{t.response_action}</td>
            </tr>
          ))}
        </tbody></table>
      </Card>
      {i.instrument_type === "inclinometer" ? <InclinometerView id={i.instrument_id} /> : <TimeSeries id={i.instrument_id} />}
    </div>
  );
}

const ms = (ts: string) => Date.parse(ts);
const day = (t: number) => new Date(t).toISOString().slice(0, 10);

function TimeSeries({ id }: { id: string }) {
  const [start, setStart] = useState("");
  const [end, setEnd] = useState("");
  const qs = new URLSearchParams();
  if (start) qs.set("start", start);
  if (end) qs.set("end", end);
  const { data, error, loading } = useApi<any>(`/api/instruments/${id}/timeseries?${qs}`);
  const pts = useMemo(
    () => (data?.points || []).map((p: any) => ({
      t: ms(p.ts_utc),
      ok: p.display_status === "VERIFIED" && p.qa_flag === "OK" ? p.value : null,
      suspect: p.display_status === "VERIFIED" && p.qa_flag !== "OK" ? p.value : null,
      ...p,
    })),
    [data]
  );
  const calChanges = useMemo(() => {
    const out: { t: number; v: number }[] = [];
    let prev: any = null;
    for (const p of pts) {
      if (prev !== null && p.cal_version !== prev) out.push({ t: p.t, v: p.cal_version });
      prev = p.cal_version;
    }
    return out;
  }, [pts]);
  if (!data) return <Loading error={error} loading={loading} />;
  const unit = data.unit;
  const s = data.summary;
  return (
    <Card title={`Time series — ${data.output_name} [${unit ?? "mixed units!"}]`} right={
      <span className="row">
        <input type="date" value={start} onChange={(e) => setStart(e.target.value)} />
        <input type="date" value={end} onChange={(e) => setEnd(e.target.value)} />
      </span>}>
      <div className="row">
        <span>{s.n} readings · {s.n_shown} shown · {s.n_withheld} withheld</span>
        <Counts obj={s.display_status} /> <Counts obj={s.qa_flag} />
        <span>calibration: {data.calibration_versions.map((c: any) => `${c.calib_id} (v${c.cal_version})`).join(", ") || "—"}</span>
      </div>
      {!unit && <div className="error">Readings have mixed or missing units ({data.units.join(", ") || "none"}); plot suppressed.</div>}
      {s.n_withheld > 0 && <div className="notice">{s.n_withheld} reading(s) are withheld from the plot (unverified formula, unapproved calibration, QC BAD or missing).</div>}
      {unit && (
        <ResponsiveContainer width="100%" height={340}>
          <ComposedChart data={pts} margin={{ top: 10, right: 30, left: 20, bottom: 10 }}>
            <CartesianGrid strokeDasharray="3 3" />
            <XAxis dataKey="t" type="number" domain={["dataMin", "dataMax"]} tickFormatter={day} scale="time" />
            <YAxis domain={["auto", "auto"]} label={{ value: `${data.output_name} (${unit})`, angle: -90, position: "insideLeft" }} />
            <Tooltip content={<TsTip unit={unit} />} />
            <Legend />
            <Line dataKey="ok" name={`verified, QC OK (${unit})`} stroke="#0b5cad" dot={false} connectNulls={false} isAnimationActive={false} />
            <Scatter dataKey="suspect" name={`verified, QC SUSPECT (${unit})`} fill="#d97706" isAnimationActive={false} />
            {data.thresholds.filter((t: any) => t.unit === unit && t.rule !== "rate_above").map((t: any) => (
              <ReferenceLine key={t.threshold_id} y={t.upper_limit ?? t.lower_limit} stroke={["", "#c9a400", "#e8590c", "#c92a2a"][t.level] || "#c92a2a"}
                             strokeDasharray="6 3" label={{ value: `L${t.level} ${t.upper_limit ?? t.lower_limit} ${t.unit}`, position: "right", fontSize: 11 }} />
            ))}
            {calChanges.map((c) => (
              <ReferenceLine key={c.t} x={c.t} stroke="#6b7a89" strokeDasharray="2 2" label={{ value: `cal v${c.v}`, position: "top", fontSize: 11 }} />
            ))}
          </ComposedChart>
        </ResponsiveContainer>
      )}
      <details>
        <summary>Readings table (latest 50)</summary>
        <table><thead><tr><th>UTC</th><th>Raw</th><th>Eng</th><th>Corrected</th><th>Cal</th><th>QC</th><th>Display</th></tr></thead><tbody>
          {(data.points as any[]).slice(-50).reverse().map((p) => (
            <tr key={p.ts_utc}>
              <td>{utc(p.ts_utc)}</td><td>{fmt(p.raw_value, 1)} {p.raw_unit}</td>
              <td>{p.eng_value === null ? <span className="withheld">withheld</span> : `${fmt(p.eng_value)} ${p.eng_unit}`}</td>
              <td>{p.value === null ? <span className="withheld">withheld</span> : `${fmt(p.value)} ${p.unit}`}</td>
              <td>{p.calib_id} v{p.cal_version}</td><td><QcBadge flag={p.qa_flag} reason={p.qa_reason} /></td>
              <td><StatusBadge status={p.display_status} /></td>
            </tr>
          ))}
        </tbody></table>
      </details>
    </Card>
  );
}

function TsTip({ active, payload, unit }: any) {
  if (!active || !payload?.length) return null;
  const p = payload[0].payload;
  return (
    <div className="card" style={{ padding: 8 }}>
      <div>{utc(p.ts_utc)}</div>
      <div>{p.value === null ? <span className="withheld">withheld</span> : <b>{fmt(p.value)} {unit}</b>}</div>
      <div>cal {p.calib_id} v{p.cal_version} · {p.formula_id} v{p.formula_version}</div>
      <div><QcBadge flag={p.qa_flag} reason={p.qa_reason} /> <StatusBadge status={p.display_status} /></div>
    </div>
  );
}

const COLORS = ["#0b5cad", "#2f9e44", "#e8590c", "#9c36b5", "#c92a2a", "#1098ad"];

function InclinometerView({ id }: { id: string }) {
  const { data: sv } = useApi<any>(`/api/instruments/${id}/incl/surveys`);
  const [sel, setSel] = useState<string[]>([]);
  const [axis, setAxis] = useState<"a" | "b">("a");
  const surveys = sv?.surveys || [];
  const chosen = sel.length ? sel : surveys.slice(-3).map((s: any) => s.survey_id);
  const { data, error } = useApi<any>(chosen.length ? `/api/instruments/${id}/incl/profile?survey_ids=${chosen.join(",")}` : null);
  const series = useMemo(() => (data?.profiles || []).map((p: any) => ({
    ...p,
    pts: p.rows.map((r: any) => ({ depth: r.depth_m, cum: r[`${axis}_cum_mm`], status: r.display_status, chk: r.qc_checksum_ok })),
  })), [data, axis]);
  return (
    <Card title="Inclinometer profile — cumulative displacement (mm) vs depth (m)" right={
      <span className="row">
        <select value={axis} onChange={(e) => setAxis(e.target.value as "a" | "b")}><option value="a">A axis</option><option value="b">B axis</option></select>
      </span>}>
      <div className="row">
        {surveys.map((s: any) => (
          <label key={s.survey_id}>
            <input type="checkbox" checked={chosen.includes(s.survey_id)}
                   onChange={(e) => setSel(e.target.checked ? [...chosen, s.survey_id] : chosen.filter((x: string) => x !== s.survey_id))} />
            {s.survey_ts.slice(0, 10)}{s.is_baseline ? " (baseline)" : ""}
          </label>
        ))}
      </div>
      <Loading error={error} />
      {series.map((p: any) => (
        <div key={p.survey_id} className="row">
          <b>{p.survey_id}</b> {utc(p.survey_ts)} · unit {p.unit} · cal {p.calib_id} v{p.cal_version} · {p.formula_id} v{p.formula_version}
          <Counts obj={p.display_status} /> <Counts obj={p.qa_flag} />
          {p.rows.some((r: any) => r.qc_checksum_ok === false) && <span className="badge warn">checksum outliers</span>}
        </div>
      ))}
      <ResponsiveContainer width="100%" height={520}>
        <LineChart layout="vertical" margin={{ top: 10, right: 30, left: 20, bottom: 20 }}>
          <CartesianGrid strokeDasharray="3 3" />
          <XAxis type="number" dataKey="cum" domain={["auto", "auto"]} label={{ value: `${axis.toUpperCase()} cumulative (mm)`, position: "insideBottom", offset: -10 }} />
          <YAxis type="number" dataKey="depth" reversed domain={[0, "dataMax"]} label={{ value: "depth (m)", angle: -90, position: "insideLeft" }} />
          <Tooltip formatter={(v: any) => (v === null ? "withheld" : `${fmt(v, 2)} mm`)} labelFormatter={(d) => `${d} m`} />
          <Legend />
          <ReferenceLine x={0} stroke="#999" />
          {series.map((p: any, n: number) => (
            <Line key={p.survey_id} data={p.pts} dataKey="cum" name={`${p.survey_ts.slice(0, 10)} (cal v${p.cal_version})`}
                  stroke={COLORS[n % COLORS.length]} dot={{ r: 2 }} isAnimationActive={false} connectNulls={false} />
          ))}
        </LineChart>
      </ResponsiveContainer>
      <div className="muted">Withheld depths (unverified/unapproved/QC BAD) are gaps. Bias corrections are proposals only and are never auto-applied.</div>
    </Card>
  );
}
