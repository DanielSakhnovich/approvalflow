import { useEffect, useState } from "react";
import { api, Thresholds as T } from "../api";

export function Thresholds() {
  const [t, setT] = useState<T | null>(null);
  const [msg, setMsg] = useState("");
  const [err, setErr] = useState("");

  const load = async () => {
    try {
      setT(await api.getThresholds());
    } catch (e) {
      setErr(String(e));
    }
  };
  useEffect(() => {
    void load();
  }, []);

  const save = async () => {
    if (!t) return;
    setErr("");
    setMsg("");
    try {
      setT(await api.putThresholds(t));
      setMsg("Saved — the next decision uses these, no redeploy.");
    } catch (e) {
      setErr(String(e));
    }
  };

  if (!t) return <div className="panel">Loading thresholds…</div>;

  const field = (key: keyof T, label: string) => (
    <div>
      <label>{label}</label>
      <input
        type="number"
        value={t[key]}
        onChange={(e) => setT({ ...t, [key]: Number(e.target.value) })}
      />
    </div>
  );

  return (
    <div className="panel">
      <h2>Autonomy thresholds</h2>
      <p className="muted">
        Configure the policy the agent enforces — changeable at runtime, no code deploy (F7).
      </p>
      <div className="row">
        {field("ceiling_cents", "Auto-approve ceiling (cents)")}
        {field("trusted_ceiling_cents", "Trusted-vendor ceiling (cents)")}
        {field("min_confidence", "Min confidence")}
      </div>
      <div className="row">
        {field("saas_monthly_cap_cents", "SaaS monthly cap (cents)")}
        {field("meals_per_attendee_cents", "Meals per attendee (cents)")}
      </div>
      <div style={{ marginTop: 12 }}>
        <button className="primary" onClick={save}>
          Save thresholds
        </button>
      </div>
      {msg && <div className="muted" style={{ marginTop: 8 }}>{msg}</div>}
      {err && <div className="err">{err}</div>}
    </div>
  );
}
