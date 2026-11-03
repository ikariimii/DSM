import { useState } from "react";
import { api, type Me } from "../api";
import { useApi } from "../hooks";
import { Card, Loading, utc } from "../components/ui";

export default function Admin({ me }: { me: Me }) {
  const { data, error, loading, reload } = useApi<any>("/api/admin/users");
  const adminDams = Object.entries(me.roles).filter(([, r]) => r === "dam_admin").map(([d]) => d);
  const [auditDam, setAuditDam] = useState<string>(me.is_admin ? "" : adminDams[0] || "");
  const { data: audit, reload: reloadAudit } = useApi<any>(`/api/admin/audit${auditDam ? `?dam_id=${auditDam}` : ""}`);
  const [err, setErr] = useState<string | null>(null);
  const [nu, setNu] = useState({ email: "", display_name: "", is_admin: false });
  const run = async (f: () => Promise<unknown>) => {
    setErr(null);
    try { await f(); reload(); reloadAudit(); } catch (e: any) { setErr(e.message); }
  };
  if (!data) return <Loading error={error} loading={loading} />;
  const dams: string[] = data.dams;
  return (
    <div>
      <h2>Users &amp; roles</h2>
      <div className="muted">Roles are per dam: viewer &lt; engineer (acknowledge alarms, draft reports) &lt; dso (close alarms, PFM reviews) &lt; dam_admin (manage roles on that dam). Identity comes from the Databricks Apps X-Forwarded-Email header.{!me.is_admin && ` You can manage roles on: ${dams.join(", ")}.`}</div>
      {err && <div className="error">{err}</div>}
      {me.is_admin && (
        <Card title="Add or update user">
          <div className="row">
            <input placeholder="email" value={nu.email} onChange={(e) => setNu({ ...nu, email: e.target.value })} />
            <input placeholder="display name" value={nu.display_name} onChange={(e) => setNu({ ...nu, display_name: e.target.value })} />
            <label><input type="checkbox" checked={nu.is_admin} onChange={(e) => setNu({ ...nu, is_admin: e.target.checked })} /> global admin</label>
            <button disabled={!nu.email} onClick={() => run(async () => { await api("/api/admin/users", { method: "POST", body: nu }); setNu({ email: "", display_name: "", is_admin: false }); })}>Save user</button>
          </div>
        </Card>
      )}
      <Card title="Role grid">
        <div style={{ overflowX: "auto" }}>
          <table>
            <thead><tr><th>User</th>{dams.map((d) => <th key={d}>{d}</th>)}{me.is_admin && <th></th>}</tr></thead>
            <tbody>
              {data.users.map((u: any) => (
                <tr key={u.email} className={u.active ? "" : "muted"}>
                  <td>{u.display_name}<div className="muted">{u.email}{u.is_admin && " · global admin"}{!u.active && " · inactive"}</div></td>
                  {dams.map((d) => (
                    <td key={d}>
                      <select value={u.roles[d] || ""} disabled={!u.active || (u.email === me.email && !me.is_admin)}
                              onChange={(e) => run(() => api("/api/admin/roles", { method: "PUT", body: { email: u.email, dam_id: d, role: e.target.value || null } }))}>
                        <option value="">none</option>
                        {data.roles.map((r: string) => <option key={r}>{r}</option>)}
                      </select>
                    </td>
                  ))}
                  {me.is_admin && <td>{u.active && u.email !== me.email && (
                    <button className="secondary" onClick={() => { if (confirm(`Deactivate ${u.email}?`)) run(() => api(`/api/admin/users/${encodeURIComponent(u.email)}`, { method: "DELETE" })); }}>Deactivate</button>)}</td>}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Card>
      <Card title="Audit log" right={
        <select value={auditDam} onChange={(e) => setAuditDam(e.target.value)}>
          {me.is_admin && <option value="">all</option>}
          {dams.map((d) => <option key={d}>{d}</option>)}
        </select>}>
        <table><thead><tr><th>Time (UTC)</th><th>Actor</th><th>Action</th><th>Dam</th><th>Target</th><th>Detail</th></tr></thead><tbody>
          {audit?.entries.map((e: any) => (
            <tr key={e.id}><td>{utc(e.ts)}</td><td>{e.actor}</td><td>{e.action}</td><td>{e.dam_id || ""}</td><td>{e.target || ""}</td>
              <td className="mono">{typeof e.detail === "string" ? e.detail : e.detail ? JSON.stringify(e.detail) : ""}</td></tr>
          ))}
        </tbody></table>
      </Card>
    </div>
  );
}
