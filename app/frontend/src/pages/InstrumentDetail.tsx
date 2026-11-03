import { Link, useParams } from "react-router-dom";
import { useMemo, useState } from "react";
import {
  CartesianGrid, ComposedChart, Legend, Line, LineChart, ReferenceLine, ResponsiveContainer, Scatter, Tooltip, XAxis, YAxis,
} from "recharts";
import { useApi } from "../hooks";
import { api } from "../api";
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
            <tr><td>Tip / top elevation</td><td>{i.tip_el_m ?? "—"} m / {i.top_el_m ?? "—"} m</td></tr>
            <tr><td>Output</td><td>{i.output_name} [{i.eng_unit}]</td></tr>
            <tr><td>Installed</td><td>{i.install_date || "—"} · {i.status}</td></tr>
            {i.computable === false && <tr><td>Computation</td><td><span className="badge warn">type has no engine formula — values withheld</span></td></tr>}
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
        <table><thead><tr><th>Calib</th><th>Version</th><th>Effective (UTC)</th><th>Method</th><th>sign_mult / offset</th><th>Parameters</th><th>Approval</th><th>Certificate</th></tr></thead><tbody>
          {data.calibrations.map((c: any) => (
            <tr key={c.calib_id}>
              <td>{c.calib_id}</td><td>v{c.cal_version} {c.is_current && <span className="badge ok">current</span>}{c.is_retired && <span className="badge warn">retired</span>}</td>
              <td>{utc(c.effective_from)} → {c.effective_to ? utc(c.effective_to) : "now"}</td>
              <td>{c.method || "—"}</td>
              <td>{c.sign_mult ?? 1} / {c.offset ?? 0} {i.eng_unit}</td>
              <td><code>{Object.entries(c.params || {}).map(([k, v]) => `${k}=${v}`).join(", ")}</code></td>
              <td>{c.approved_by ? <span className="badge ok">{c.approved_by} {utc(c.approved_at)}</span> : <span className="badge warn">NOT APPROVED — values withheld</span>}
                {(c.calibration_issues || []).length > 0 && <div className="badge warn" title="values withheld">calibration issues: {c.calibration_issues.join("; ")}</div>}</td>
              <td>{c.certificate_uri || "—"}</td>
            </tr>
          ))}
        </tbody></table>
      </Card>
      <Card title="Thresholds (current)">
        {data.thresholds.length === 0 && <div className="muted">No thresholds defined.</div>}
        <table><tbody>
          {data.thresholds.map((t: any) => (
            <tr key={t.threshold_id}>
              <td>L{t.level}</td><td>{t.limit_type} / {t.direction}</td>
              <td>{t.rule_label} {t.evaluated ? null : <span className="badge warn">not evaluated</span>}</td>
              <td>{t.value} {t.unit}</td>
              <td>{t.pfm_id || "—"}</td><td>{t.required_actions}</td>
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
            {data.thresholds.filter((t: any) => t.unit === unit && t.evaluated && t.limit_type === "magnitude").flatMap((t: any) =>
              (t.direction === "abs" ? [t.value, -t.value] : [t.value]).map((y: number) => (
                <ReferenceLine key={`${t.threshold_id}:${y}`} y={y} stroke={["", "#c9a400", "#e8590c", "#c92a2a"][t.level] || "#c92a2a"}
                               strokeDasharray="6 3" label={{ value: `L${t.level} ${t.direction === "abs" ? "|x| " : ""}${y} ${t.unit}`, position: "right", fontSize: 11 }} />
              )))}
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
  const { data, error, reload } = useApi<any>(chosen.length ? `/api/instruments/${id}/incl/profile?survey_ids=${chosen.join(",")}` : null);
  const series = useMemo(() => (data?.profiles || []).map((p: any) => ({
    ...p,
    pts: p.rows.map((r: any) => ({ depth: r.depth_m, cum: r[`cum_${axis}`], status: r.display_status, chk: r.qc_checksum_ok })),
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
          <b>{p.survey_id}</b> {utc(p.survey_ts)} · unit {p.unit} · {p.calib_id ? <>cal {p.calib_id} v{p.cal_version} · </> : null}{p.formula_id} v{p.formula_version}
          {" "}<span className={p.formula_verified ? "badge muted" : "badge warn"}>{p.formula_verified ? "formula verified" : "formula NOT verified"}</span>
          {p.qc_rule_version && <> · QC rule {Array.isArray(p.qc_rule_version) ? p.qc_rule_version.join(", ") : p.qc_rule_version}</>}
          {p.installation_version && <> · installation {Array.isArray(p.installation_version) ? p.installation_version.map(utc).join(", ") : utc(p.installation_version)}</>}
          {p.installation_approved === true && <span className="badge muted">installation approved</span>}
          {p.installation_approved === false && <span className="badge bad">installation not approved — values withheld</span>}
          {p.probe_constants && <div className="muted">probe constants: {Array.isArray(p.probe_constants) ? p.probe_constants.join(" | ") : p.probe_constants}</div>}
          <Counts obj={p.display_status} /> <Counts obj={p.qa_flag} />
          {p.rows.some((r: any) => r.qc_checksum_ok === false) && <span className="badge warn">checksum outliers</span>}
          {(p.correction_basis || []).map((b: string) => <span key={b} className="badge muted">{b}</span>)}
          {(p.corrections || []).length > 0 && <span className="badge warn">{p.corrections.length} correction record(s) — affected depths withheld (unverified)</span>}
          {(p.qa_reasons || []).map((q: any) => (
            <span key={q.code} className={q.severity === "BAD" ? "badge bad" : q.severity === "SUSPECT" || q.severity === "MISSING" ? "badge warn" : "badge muted"}
                  title={q.label || "code not in app vocabulary (shown verbatim)"}>{q.code}</span>
          ))}
          <BiasQc bq={p.bias_qc} iid={id} sid={p.survey_id} signoffs={p.bias_signoffs || []} onReviewed={reload} />
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
      {data?.profiles?.[0]?.correction_note && <div className="notice">{data.profiles[0].correction_note}</div>}
      <div className="muted">Withheld depths (unverified/unapproved/QC BAD/unverified correction) are gaps. Values are uncorrected: no bias correction is applied. Engine QA-020 bias proposals are shown UNVERIFIED and are never applied.</div>
    </Card>
  );
}

/** Engine between-set bias QC (SCHEMA_CONTRACT §5.5 v1.4, api_contract §4.1). Display only: UNVERIFIED, never applied. */
function BiasQc({ bq, iid, sid, signoffs, onReviewed }: { bq?: any; iid: string; sid: string; signoffs: any[]; onReviewed: () => void }) {
  if (!bq) return null;
  const axes = Object.entries(bq.axes || {}).filter(([, a]: any) => a.suspect_flag || a.status || (a.proposals || []).length);
  if (!axes.length && !bq.stable_zone_assumed) return null;
  return (
    <div className="notice">
      <b>Bias QC (engine{bq.rule ? `, ${bq.rule}` : ""}) — UNVERIFIED, never applied by the app</b>
      {axes.map(([ax, a]: any) => (
        <div key={ax}>
          Axis {ax}: {a.suspect_flag && <span className="badge warn">suspect: checksum shift vs reference (investigate)</span>}{" "}
          {a.status && <span className={a.status === "BAD_RESURVEY" ? "badge bad" : "badge warn"}>{a.status}{a.status === "SUSPECT_CROSSCHECK" ? " (proposal ranked SUSPECT: cross-check disagrees; review)" : ""}</span>}{" "}
          {(a.status === "PROPOSED" || a.status === "SUSPECT_CROSSCHECK") && (a.proposed_c !== null
            ? <span><span className="badge warn">UNVERIFIED</span> proposed c = {fmt(a.proposed_c, 2)} {a.proposed_c_unit} · record {a.proposal_record_id} · <b>not applied</b>
                {a.evidence?.n && <span className="muted"> · n={a.evidence.n} sd={a.evidence.sd}{a.evidence.se ? ` se=${a.evidence.se}` : ""} reference {a.evidence.reference_survey_id}{a.evidence.stable_zone_top_m ? ` · stable zone from ${a.evidence.stable_zone_top_m} m` : ""}{a.evidence.params_unverified === "True" ? " · params UNVERIFIED" : ""}</span>}</span>
            : <span className="muted">proposed c not recorded in ref.incl_correction — hidden</span>)}
        </div>
      ))}
      {Object.entries(bq.axes || {}).map(([ax, a]: any) => (a.proposals || []).length > 0 && (
        <table key={`p-${ax}`} className="small">
          <caption>Axis {ax} proposals (AGREE first) — UNVERIFIED, not applied by the app</caption>
          <thead><tr><th>record</th><th>ZA ({a.proposals[0].za_unit})</th><th>|ΔD| (mm)</th><th>|bs| (mm)</th><th>cross-check</th><th>status</th><th>approval</th><th>rule</th><th>sign-offs (append-only)</th></tr></thead>
          <tbody>{a.proposals.map((q: any) => (
            <tr key={q.record_id}>
              <td>{q.record_id}</td><td>{fmt(q.za, 2)}</td><td>{fmt(q.abs_dD, 2)}</td><td>{fmt(q.abs_bs, 2)}</td>
              <td><span className={q.crosscheck_verdict === "AGREE" ? "badge muted" : "badge warn"}>{q.crosscheck_verdict ?? "—"}</span>{q.crosscheck_diff !== null && q.crosscheck_diff !== undefined ? ` Δ${fmt(q.crosscheck_diff, 2)}/tol ${fmt(q.crosscheck_tol, 2)}` : ""}</td>
              <td>{q.status}</td>
              <td>{q.integrity_error
                    ? <span className="badge bad" title="Stored record violates the approval rule (applied without an independent review); values withheld">DATA INTEGRITY ERROR: {q.integrity_error}</span>
                    : <span className={q.approval_status === "approved" ? "badge muted" : "badge warn"}
                        title={q.reviewed_by ? `reviewed by ${q.reviewed_by} ${q.reviewed_utc ?? ""}` : "not reviewed"}>{q.approval_status}</span>}{" "}
                  <span className="badge warn">{q.display_status}</span></td>
              <td>{q.rule ?? "—"}</td>
              <td><SignOffs rows={signoffs.filter((x: any) => x.subject_id === q.record_id)} />
                {!q.integrity_error && q.approval_status === "proposed" && <ReviewForm iid={iid} sid={sid} rid={q.record_id} onDone={onReviewed} />}</td>
            </tr>))}</tbody>
        </table>
      ))}
      {bq.stable_zone_assumed && <div><span className="badge muted">STABLE_ZONE_ASSUMED</span> stable zone not set; bottom intervals assumed</div>}
      <div className="muted">{bq.note}</div>
    </div>
  );
}

/** QA-039 sign-offs: each is an append-only row; the hash is re-verified against the current record by the server. */
function SignOffs({ rows }: { rows: any[] }) {
  if (!rows.length) return <span className="muted">none</span>;
  return <>{rows.map((r) => (
    <div key={r.review_id} className="small">
      <span className={r.decision === "approve" ? "badge muted" : "badge warn"}>{r.decision}</span> {r.reviewer_id}{r.reviewer_pe_license ? ` (PE ${r.reviewer_pe_license})` : ""} · {utc(r.signed_ts)}
      {" "}<span className={r.signature_verified ? "badge muted" : "badge bad"} title={`sha256 ${r.signature_hash}`}>{r.signature_verified ? "signature verified" : "SIGNATURE MISMATCH — record changed after sign-off"}</span>
      <div className="muted">{r.comment}</div>
    </div>))}</>;
}

/** Records a signed decision only; the app never applies a correction (the engine applies approved records). */
function ReviewForm({ iid, sid, rid, onDone }: { iid: string; sid: string; rid: string; onDone: () => void }) {
  const [rationale, setRationale] = useState("");
  const [pe, setPe] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const send = (decision: "approve" | "reject") => {
    setErr(null);
    api(`/api/instruments/${iid}/incl/surveys/${sid}/bias-proposals/${rid}/review`, {
      method: "POST", body: { decision, rationale, reviewer_pe_license: pe || null },
    }).then(() => { setRationale(""); onDone(); }).catch((e) => setErr(String(e.message || e)));
  };
  return (
    <div className="row small">
      <input placeholder="rationale (required)" value={rationale} onChange={(e) => setRationale(e.target.value)} />
      <input placeholder="PE licence (optional)" value={pe} onChange={(e) => setPe(e.target.value)} size={10} />
      <button disabled={!rationale.trim()} onClick={() => send("approve")}>Sign approve</button>
      <button disabled={!rationale.trim()} onClick={() => send("reject")}>Sign reject</button>
      {err && <span className="badge bad">{err}</span>}
    </div>
  );
}
