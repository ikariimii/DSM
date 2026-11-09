import { useEffect, useState } from "react";
import { api } from "../api";
import { useApi } from "../hooks";
import { Card, Loading, utc } from "../components/ui";

type Col = { key: string; label?: string; type?: "text" | "number" | "select" | "json" | "date" | "checkbox"; options?: string[] };

const TITLES: Record<string, string> = {
  details: "1. Dam details", structures: "2. Structures", instruments: "3. Instruments", calibrations: "4. Calibrations",
  formulas: "5. Formula check", thresholds: "6. Thresholds", data_sources: "7. Data sources", users: "8. Users & roles",
  validate: "9. Validate", golive: "10. Go live",
};

export default function Onboarding() {
  const { data: meta } = useApi<any>("/api/onboarding/steps");
  const { data: list, reload } = useApi<{ drafts: any[] }>("/api/onboarding/drafts");
  const [draftId, setDraftId] = useState<string | null>(null);
  if (!meta) return <Loading loading />;
  if (!draftId) {
    return (
      <div>
        <h2>Onboard a dam</h2>
        <Card title="Drafts" right={<button onClick={async () => { const d = await api("/api/onboarding/drafts", { method: "POST", body: {} }); reload(); setDraftId(d.draft_id); }}>New draft</button>}>
          <table><thead><tr><th>Draft</th><th>Dam</th><th>Status</th><th>Updated (UTC)</th><th></th></tr></thead><tbody>
            {list?.drafts.map((d) => (
              <tr key={d.draft_id}><td>{d.draft_id.slice(0, 8)}</td><td>{d.dam_id || "—"} {d.name || ""}</td><td>{d.status}</td><td>{utc(d.updated_at)}</td>
                <td><button className="secondary" onClick={() => setDraftId(d.draft_id)}>open</button></td></tr>
            ))}
          </tbody></table>
        </Card>
      </div>
    );
  }
  return <Wizard meta={meta} draftId={draftId} onExit={() => { setDraftId(null); reload(); }} />;
}

function Wizard({ meta, draftId, onExit }: { meta: any; draftId: string; onExit: () => void }) {
  const { data: draft, reload } = useApi<any>(`/api/onboarding/drafts/${draftId}`);
  const [step, setStep] = useState(0);
  const [msg, setMsg] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  if (!draft) return <Loading loading />;
  const steps: string[] = meta.steps;
  const name = steps[step];
  const data = draft.data || {};
  const readOnly = draft.status !== "draft";
  const save = async (value: unknown) => {
    setErr(null); setMsg(null);
    try {
      await api(`/api/onboarding/drafts/${draftId}/steps/${name}`, { method: "PUT", body: value });
      setMsg("saved");
      reload();
    } catch (e: any) { setErr(e.message); }
  };
  const fnames = meta.formulas.map((f: any) => `${f.formula_id}`);
  const instIds = (data.instruments || []).map((i: any) => i.instrument_id);
  const types = Object.keys(meta.type_units);
  return (
    <div>
      <h2>Onboarding draft {draftId.slice(0, 8)} {data.details?.dam_id && `— ${data.details.dam_id}`} <span className="badge muted">{draft.status}</span></h2>
      <div className="row"><button className="secondary" onClick={onExit}>← all drafts</button></div>
      <div className="steps">
        {steps.map((s, n) => (
          <button key={s} className={n === step ? "current" : data[s] ? "done" : ""} onClick={() => { setStep(n); setMsg(null); setErr(null); }}>{TITLES[s] || s}</button>
        ))}
      </div>
      {readOnly && <div className="notice">This draft is {draft.status}; it can no longer be edited.</div>}
      {msg && <div className="muted">{msg}</div>}
      {err && <div className="error">{err}</div>}
      <Card title={TITLES[name]}>
        {name === "details" && <DetailsForm value={data.details || {}} hazard={meta.hazard_classes} onSave={save} disabled={readOnly} />}
        {name === "structures" && <Rows value={data.structures || []} onSave={save} disabled={readOnly} cols={[
          { key: "structure_id" }, { key: "name" }, { key: "structure_type" }, { key: "station_from_m", type: "number" }, { key: "station_to_m", type: "number" }]} />}
        {name === "instruments" && (
          <>
            <CsvImport meta={meta} onRows={(rows) => save(rows)} disabled={readOnly} />
            <Rows value={data.instruments || []} onSave={save} disabled={readOnly} cols={[
              { key: "instrument_id" }, { key: "structure_id", type: "select", options: (data.structures || []).map((s: any) => s.structure_id) },
              { key: "instrument_type", type: "select", options: types }, { key: "subtype" },
              { key: "formula_id", type: "select", options: fnames }, { key: "formula_version" },
              { key: "eng_unit" }, { key: "output_name" }, { key: "station_m", type: "number" }, { key: "offset_m", type: "number" },
              { key: "tip_el_m", type: "number" }, { key: "top_el_m", type: "number" }, { key: "install_date", type: "date" }]} />
            <div className="muted">Allowed units by type: {Object.entries(meta.type_units).map(([k, v]: any) => `${k}: ${v.join("/")}`).join(" · ")}</div>
          </>
        )}
        {name === "calibrations" && (
          <>
            <div className="muted">Params as JSON, e.g. {`{"G":0.1795,"R0":9082,"K":0.13,"T0":12,"tip_el_m":266}`}. sign_mult, offset and method are calibration fields, not params (SCHEMA_CONTRACT §10 Q3). Store |G| and set sign_mult −1 for a negative manufacturer gauge factor. Required params per formula: {meta.formulas.map((f: any) => `${f.formula_id}: ${f.required_params.join(",") || "—"}`).join(" · ")}</div>
            <Rows value={data.calibrations || []} onSave={save} disabled={readOnly} cols={[
              { key: "instrument_id", type: "select", options: instIds }, { key: "cal_version", type: "number" },
              { key: "effective_from", label: "effective_from (UTC)" }, { key: "method", type: "select", options: ["linear", "poly"] },
              { key: "sign_mult", type: "select", options: ["1", "-1"] }, { key: "offset", type: "number" },
              { key: "params", type: "json" }, { key: "certificate_uri" }, { key: "approved_by" }, { key: "approved_at" }]} />
          </>
        )}
        {name === "formulas" && <FormulaCheck draftId={draftId} checks={data.formulas?.checks} onDone={reload} disabled={readOnly} />}
        {name === "thresholds" && <Rows value={data.thresholds || []} onSave={save} disabled={readOnly} cols={[
          { key: "instrument_id", type: "select", options: instIds }, { key: "level", type: "select", options: ["1", "2", "3"] },
          { key: "limit_type", type: "select", options: ["magnitude", "rate", "cyclic"] },
          { key: "direction", type: "select", options: ["upper", "lower", "abs"] }, { key: "value", type: "number" },
          { key: "unit" }, { key: "pfm_id" }, { key: "required_actions" },
          { key: "dssmp_version_id", label: "DSSMP version *" },
          { key: "tier", label: "tier *", type: "select", options: ["AL1", "AL2", "AL3", "AL4", "AL5", "AL6", "AL7", "AL8", "AL9", "THRESHOLD", "DESIGN_BASIS"] },
          { key: "deadband", label: "deadband * (rule unit)", type: "number" },
          { key: "persistence_on", label: "persistence on * (readings)", type: "number" },
          { key: "persistence_off", label: "persistence off * (readings)", type: "number" },
          { key: "cdse_ack_required", label: "CDSE ack required", type: "checkbox" }]} />}
        {name === "thresholds" && <div className="muted">* Required to go live (ref.threshold NOT NULL; no defaults are filled in): DSSMP version the limit is set under; tier (AL1..ALn, THRESHOLD or DESIGN_BASIS); deadband = hysteresis in the rule's own unit (≥ 0); persistence on / off = consecutive readings to raise / to clear (whole number ≥ 1).</div>}
        {name === "thresholds" && <div className="muted">"CDSE ack required" (default off): acknowledging any alarm from this rule, at any level, needs the dam's named CDSE (ref.dam.cdse_id). Levels 2 and 3 always need the CDSE. Changing it is recorded in the audit log.</div>}
        {name === "thresholds" && <div className="muted">Cyclic limits are stored but not evaluated by the alarm machine; abs limits are evaluated as |value| &gt; limit.</div>}
        {name === "data_sources" && <Rows value={data.data_sources || []} onSave={save} disabled={readOnly} cols={[
          { key: "instrument_id", type: "select", options: instIds }, { key: "source_type", type: "select", options: meta.source_types },
          { key: "path" }, { key: "table" }, { key: "column" }, { key: "raw_unit" }]} />}
        {name === "users" && <Rows value={data.users || []} onSave={save} disabled={readOnly} cols={[
          { key: "email" }, { key: "display_name" }, { key: "role", type: "select", options: ["viewer", "engineer", "dso", "dam_admin"] }]} />}
        {name === "validate" && <Validate draftId={draftId} />}
        {name === "golive" && <GoLive draftId={draftId} disabled={readOnly} onDone={reload} />}
      </Card>
      <div className="row">
        <button className="secondary" disabled={step === 0} onClick={() => setStep(step - 1)}>Back</button>
        <button className="secondary" disabled={step === steps.length - 1} onClick={() => setStep(step + 1)}>Next</button>
      </div>
    </div>
  );
}

function DetailsForm({ value, hazard, onSave, disabled }: { value: any; hazard: string[]; onSave: (v: any) => void; disabled: boolean }) {
  const [v, setV] = useState<any>(value);
  useEffect(() => setV(value), [value]);
  const f = (k: string, label: string, type = "text") => (
    <tr><td>{label}</td><td><input type={type} value={v[k] ?? ""} disabled={disabled}
      onChange={(e) => setV({ ...v, [k]: type === "number" && e.target.value !== "" ? Number(e.target.value) : e.target.value })} /></td></tr>
  );
  return (
    <div>
      <table className="kv"><tbody>
        {f("dam_id", "Dam ID (2-8 upper-case)")}{f("name", "Name")}{f("dam_type", "Dam type")}{f("owner", "Owner")}{f("nid_id", "NID ID")}{f("state", "State")}
        <tr><td>Hazard class</td><td><select value={v.hazard_class || ""} disabled={disabled} onChange={(e) => setV({ ...v, hazard_class: e.target.value })}>
          <option value="">—</option>{hazard.map((h) => <option key={h}>{h}</option>)}</select></td></tr>
        {f("lat", "Latitude (deg)", "number")}{f("lon", "Longitude (deg)", "number")}{f("height_m", "Height (m)", "number")}
        {f("crest_elev_m", "Crest elevation (m)", "number")}{f("normal_pool_m", "Normal pool (m)", "number")}{f("datum", "Vertical datum (e.g. NAVD88)")}
      </tbody></table>
      <button disabled={disabled} onClick={() => onSave(v)}>Save details</button>
    </div>
  );
}

function Rows({ value, cols, onSave, disabled }: { value: any[]; cols: Col[]; onSave: (v: any[]) => void; disabled: boolean }) {
  const [rows, setRows] = useState<any[]>(value);
  const [bad, setBad] = useState<string | null>(null);
  useEffect(() => setRows(value), [value]);
  const set = (n: number, k: string, raw: string, c: Col) => {
    const r = [...rows];
    let v: any = raw;
    if (c.type === "number") v = raw === "" ? null : Number(raw);
    if (k === "level") v = Number(raw);
    r[n] = { ...r[n], [k]: v };
    setRows(r);
  };
  const submit = () => {
    try {
      const out = rows.map((r) => {
        const o = { ...r };
        for (const c of cols) if (c.type === "json" && typeof o[c.key] === "string") o[c.key] = o[c.key] ? JSON.parse(o[c.key]) : null;
        return o;
      });
      setBad(null);
      onSave(out);
    } catch (e: any) { setBad(`Invalid JSON: ${e.message}`); }
  };
  return (
    <div style={{ overflowX: "auto" }}>
      <table>
        <thead><tr>{cols.map((c) => <th key={c.key}>{c.label || c.key}</th>)}<th></th></tr></thead>
        <tbody>
          {rows.map((r, n) => (
            <tr key={n}>
              {cols.map((c) => {
                const val = c.type === "json" && typeof r[c.key] !== "string" ? (r[c.key] == null ? "" : JSON.stringify(r[c.key])) : r[c.key] ?? "";
                return (
                  <td key={c.key}>
                    {c.type === "checkbox" ? (
                      <input type="checkbox" checked={r[c.key] === true} disabled={disabled}
                             onChange={(e) => { const nr = [...rows]; nr[n] = { ...nr[n], [c.key]: e.target.checked }; setRows(nr); }} />
                    ) : c.type === "select" ? (
                      <select value={String(val)} disabled={disabled} onChange={(e) => set(n, c.key, e.target.value, c)}>
                        <option value="">—</option>{c.options?.map((o) => <option key={o}>{o}</option>)}
                      </select>
                    ) : (
                      <input type={c.type === "number" ? "number" : c.type === "date" ? "date" : "text"} value={String(val)} disabled={disabled}
                             size={c.type === "json" ? 40 : 12} onChange={(e) => set(n, c.key, e.target.value, c)} />
                    )}
                  </td>
                );
              })}
              <td><button className="secondary" disabled={disabled} onClick={() => setRows(rows.filter((_, i) => i !== n))}>✕</button></td>
            </tr>
          ))}
        </tbody>
      </table>
      {bad && <div className="error">{bad}</div>}
      <div className="row">
        <button className="secondary" disabled={disabled} onClick={() => setRows([...rows, {}])}>Add row</button>
        <button disabled={disabled} onClick={submit}>Save step</button>
      </div>
    </div>
  );
}

function CsvImport({ meta, onRows, disabled }: { meta: any; onRows: (rows: any[]) => void; disabled: boolean }) {
  const [csv, setCsv] = useState("");
  const [issues, setIssues] = useState<any[]>([]);
  return (
    <details>
      <summary>Import instruments from CSV</summary>
      <div className="muted">Columns: {meta.csv_columns.join(", ")} (required: {meta.csv_required.join(", ")})</div>
      <textarea rows={5} value={csv} onChange={(e) => setCsv(e.target.value)} disabled={disabled} />
      <button disabled={disabled || !csv.trim()} onClick={async () => {
        const r = await api("/api/onboarding/parse-csv", { method: "POST", body: { csv } });
        setIssues(r.issues);
        if (!r.issues.some((i: any) => i.severity === "error")) onRows(r.rows);
      }}>Parse &amp; replace instruments</button>
      <Issues issues={issues} />
    </details>
  );
}

function Issues({ issues }: { issues: any[] }) {
  if (!issues.length) return null;
  return (
    <table><tbody>
      {issues.map((i, n) => (
        <tr key={n}><td><span className={`badge ${i.severity === "error" ? "bad" : "warn"}`}>{i.severity}</span></td><td>{i.step}</td><td>{i.ref || ""}</td><td>{i.message}</td></tr>
      ))}
    </tbody></table>
  );
}

function FormulaCheck({ draftId, checks, onDone, disabled }: { draftId: string; checks: any; onDone: () => void; disabled: boolean }) {
  const [err, setErr] = useState<string | null>(null);
  return (
    <div>
      <div className="muted">Runs every published test vector for each formula version used by the draft's instruments. Instruments on unverified formulas will have their values withheld after go-live.</div>
      <button disabled={disabled} onClick={async () => {
        setErr(null);
        try { await api(`/api/onboarding/drafts/${draftId}/formulas/check`, { method: "POST" }); onDone(); } catch (e: any) { setErr(e.message); }
      }}>Run formula checks</button>
      {err && <div className="error">{err}</div>}
      {checks && (
        <table><thead><tr><th>Formula</th><th>Engine registry</th><th>App test vectors</th></tr></thead><tbody>
          {Object.values(checks).map((c: any) => (
            <tr key={`${c.formula_id}:${c.version}`}><td>{c.formula_id} v{c.version}</td>
              <td>{c.engine_verified ? <span className="badge ok">verified</span> : <span className="badge warn">unverified</span>}</td>
              <td>{!c.app_check.known ? <span className="badge bad">unknown formula</span> : c.app_check.verified
                ? <span className="badge ok">{c.app_check.results.length} vector(s) pass</span>
                : <span className="badge warn">{c.app_check.results.length ? "failing vectors" : "no test vectors"}</span>}</td></tr>
          ))}
        </tbody></table>
      )}
    </div>
  );
}

function Validate({ draftId }: { draftId: string }) {
  const [res, setRes] = useState<any>(null);
  return (
    <div>
      <button onClick={async () => setRes(await api(`/api/onboarding/drafts/${draftId}/validate`, { method: "POST" }))}>Validate draft</button>
      {res && (
        <div>
          <div className={res.ok ? "badge ok" : "badge bad"}>{res.ok ? "no blocking errors" : `${res.errors} error(s)`}</div> <span className="badge warn">{res.warnings} warning(s)</span>
          <Issues issues={res.issues} />
        </div>
      )}
    </div>
  );
}

function GoLive({ draftId, disabled, onDone }: { draftId: string; disabled: boolean; onDone: () => void }) {
  const [res, setRes] = useState<any>(null);
  const [err, setErr] = useState<any>(null);
  return (
    <div>
      <div className="muted">Global admins only. Writes a publication request (demo: applied immediately; production: queued for the reference-data job) and grants the listed roles.</div>
      <button disabled={disabled} onClick={async () => {
        setErr(null);
        try { setRes(await api(`/api/onboarding/drafts/${draftId}/golive`, { method: "POST" })); onDone(); }
        catch (e: any) { setErr(e.detail?.issues ? e.detail : { message: e.message }); }
      }}>Go live</button>
      {err && <div className="error">{err.message}</div>}
      {err?.issues && <Issues issues={err.issues} />}
      {res && <div className="notice">Publication {res.publication.pub_id} — status <b>{res.publication.status}</b>{res.warnings.length > 0 && `, ${res.warnings.length} warning(s)`}</div>}
    </div>
  );
}
