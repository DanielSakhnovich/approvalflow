import { useEffect, useState } from "react";
import { api, dollars } from "../api";

const MONEY_KEYS = new Set(["paid_auto_cents", "paid_human_cents"]);
const LABELS: Record<string, string> = {
  submitted: "Submitted",
  decided_auto_approve: "Auto-approved",
  decided_human_review: "Escalated to human",
  decided_reject: "Rejected",
  decided_duplicate: "Duplicates",
  verdict_approved: "Human-approved",
  verdict_rejected: "Human-rejected",
  verdict_needs_info: "Sent back",
  paid: "Paid",
  payment_failed: "Payment failed",
  paid_auto_cents: "Money auto-approved",
  paid_human_cents: "Money human-approved",
};

export function Dashboard() {
  const [counters, setCounters] = useState<Record<string, number>>({});
  const [err, setErr] = useState("");

  const load = async () => {
    try {
      setCounters(await api.dashboard());
    } catch (e) {
      setErr(String(e));
    }
  };

  useEffect(() => {
    void load();
    const t = setInterval(load, 3000);
    return () => clearInterval(t);
  }, []);

  const keys = Object.keys(counters).sort();

  return (
    <div className="panel">
      <h2>Dashboard</h2>
      <p className="muted">Throughput, auto-approval vs escalation, and money moved.</p>
      {err && <div className="err">{err}</div>}
      {keys.length === 0 && <p className="muted">no activity yet</p>}
      {keys.map((k) => (
        <div className="stat" key={k}>
          <span>{LABELS[k] ?? k}</span>
          <b>{MONEY_KEYS.has(k) ? dollars(counters[k]) : counters[k]}</b>
        </div>
      ))}
    </div>
  );
}
