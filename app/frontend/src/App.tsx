import { NavLink, Route, Routes } from "react-router-dom";
import { demoUser, setDemoUser, type Me } from "./api";
import { useApi } from "./hooks";
import { Loading } from "./components/ui";
import Portfolio from "./pages/Portfolio";
import Dam from "./pages/Dam";
import InstrumentDetail from "./pages/InstrumentDetail";
import Alarms from "./pages/Alarms";
import Dssmp from "./pages/Dssmp";
import AlarmRules from "./pages/AlarmRules";
import Reports from "./pages/Reports";
import Onboarding from "./pages/Onboarding";
import Admin from "./pages/Admin";
import Formulas from "./pages/Formulas";

export default function App() {
  const { data: me, error } = useApi<Me>("/api/me");
  return (
    <div className="app">
      <header className="top">
        <div className="brand">Dam Safety Monitoring</div>
        <nav>
          <NavLink to="/" end>Portfolio</NavLink>
          <NavLink to="/alarms">Alarms</NavLink>
          <NavLink to="/formulas">Formulas</NavLink>
          {me?.can_onboard && <NavLink to="/onboarding">Onboard a dam</NavLink>}
          {(me?.is_admin || Object.values(me?.roles || {}).includes("dam_admin")) && <NavLink to="/admin">Users &amp; roles</NavLink>}
        </nav>
        <div className="who">
          {me?.demo_mode && <span className="badge warn">DEMO DATA</span>}{" "}
          {me?.demo_mode && me.demo_users ? (
            <select
              value={demoUser() || me.email}
              onChange={(e) => {
                setDemoUser(e.target.value);
                window.location.reload();
              }}
              title="Demo only: act as another seeded user to exercise per-dam ACLs"
            >
              {me.demo_users.map((u) => (
                <option key={u.email} value={u.email}>{u.display_name}</option>
              ))}
            </select>
          ) : (
            <span>{me?.email}</span>
          )}
        </div>
      </header>
      <main>
        <Loading error={error} />
        {me && !me.known && (
          <div className="error">
            {me.email} has no access yet. Ask a dam administrator to grant you a role.
          </div>
        )}
        {me && (
          <Routes>
            <Route path="/" element={<Portfolio />} />
            <Route path="/dams/:damId" element={<Dam me={me} />} />
            <Route path="/dams/:damId/dssmp" element={<Dssmp />} />
            <Route path="/dams/:damId/alarm-rules" element={<AlarmRules />} />
            <Route path="/dams/:damId/reports" element={<Reports me={me} />} />
            <Route path="/instruments/:instrumentId" element={<InstrumentDetail />} />
            <Route path="/alarms" element={<Alarms />} />
            <Route path="/formulas" element={<Formulas />} />
            <Route path="/onboarding" element={<Onboarding />} />
            <Route path="/admin" element={<Admin me={me} />} />
            <Route path="*" element={<div>Not found</div>} />
          </Routes>
        )}
      </main>
      <footer>
        Values are shown only when the formula version is verified, the calibration is approved and QC is not BAD/MISSING.
        All times UTC.
      </footer>
    </div>
  );
}
