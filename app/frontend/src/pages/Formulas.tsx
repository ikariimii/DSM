import { useApi } from "../hooks";
import { Card, Loading, fmt } from "../components/ui";

export default function Formulas() {
  const { data, error, loading } = useApi<{ formulas: any[] }>("/api/formulas");
  return (
    <div>
      <h2>Formula registry</h2>
      <div className="muted">A formula version is "verified" only when it has published test vectors and every vector passes. Values computed with unverified formulas are withheld everywhere.</div>
      <Loading error={error} loading={loading} />
      {data?.formulas.map((f) => (
        <Card key={`${f.formula_id}:${f.formula_version}`} title={`${f.formula_id} v${f.formula_version}`}
              right={f.verified ? <span className="badge ok">verified ({f.verified_by})</span> : <span className="badge warn">UNVERIFIED</span>}>
          <div>{f.description}</div>
          <div className="muted">output {f.output_name} [{f.unit}] · inputs {(f.inputs || []).join(", ")} · required params {(f.required_params || []).join(", ")}</div>
          {f.app_check?.results?.length > 0 && (
            <table><thead><tr><th>Test vector</th><th>Source</th><th>Expected</th><th>Computed</th><th>Tol</th><th>Pass</th></tr></thead><tbody>
              {f.app_check.results.map((r: any) => (
                <tr key={r.name}><td>{r.name}</td><td>{r.source}</td><td>{r.expected}</td><td>{r.error ? <span className="warn-text">{r.error}</span> : fmt(r.computed, 4)}</td><td>{r.tol}</td>
                  <td>{r.pass ? <span className="badge ok">pass</span> : <span className="badge bad">fail</span>}</td></tr>
              ))}
            </tbody></table>
          )}
          {f.app_check && f.app_check.verified !== f.verified && <div className="notice">App-side check disagrees with the engine registry — investigate.</div>}
        </Card>
      ))}
    </div>
  );
}
