import { useEffect, useState } from "react";
import { api, dollars, QueueItem } from "../api";

export function ApproverQueue() {
  const [items, setItems] = useState<QueueItem[]>([]);
  const [approver, setApprover] = useState("lena.schmidt@northwind.example");
  const [err, setErr] = useState("");

  const load = async () => {
    setErr("");
    try {
      const { items } = await api.queue();
      setItems(items);
    } catch (e) {
      setErr(String(e));
    }
  };

  useEffect(() => {
    void load();
    const t = setInterval(load, 3000);
    return () => clearInterval(t);
  }, []);

  const decide = async (id: string, verdict: string) => {
    setErr("");
    const comment =
      verdict === "needs_info" ? window.prompt("What info is needed?") ?? "" : "";
    try {
      await api.verdict(id, verdict, approver, comment);
      await load();
    } catch (e) {
      setErr(String(e));
    }
  };

  return (
    <div className="panel">
      <h2>Approver queue</h2>
      <p className="muted">
        Only the items the system escalated — each with the agent&apos;s recommendation,
        confidence, and the rules it cited. Approve what&apos;s safe; the system already
        handled the boring majority.
      </p>
      <label>Acting as approver</label>
      <input value={approver} onChange={(e) => setApprover(e.target.value)} />
      {err && <div className="err">{err}</div>}
      <table style={{ marginTop: 16 }}>
        <thead>
          <tr>
            <th>Invoice</th>
            <th>Amount</th>
            <th>Recommendation</th>
            <th>Cited rules</th>
            <th>Actions</th>
          </tr>
        </thead>
        <tbody>
          {items.map((it) => (
            <tr key={it.invoiceId}>
              <td className="mono">{it.invoiceId}</td>
              <td>{dollars(it.usdCents)}</td>
              <td>
                {it.recommendation}
                {it.confidence != null && (
                  <span className="muted"> ({(it.confidence * 100).toFixed(0)}%)</span>
                )}
                <div className="muted">{it.reasoning}</div>
              </td>
              <td className="mono">{it.violations.join(", ") || "—"}</td>
              <td style={{ whiteSpace: "nowrap" }}>
                <button className="sm approve" onClick={() => decide(it.invoiceId, "approved")}>
                  Approve
                </button>
                <button className="sm reject" onClick={() => decide(it.invoiceId, "rejected")}>
                  Reject
                </button>
                <button className="sm info" onClick={() => decide(it.invoiceId, "needs_info")}>
                  Send back
                </button>
              </td>
            </tr>
          ))}
          {items.length === 0 && (
            <tr>
              <td colSpan={5} className="muted">
                queue is empty — nothing needs a human right now
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </div>
  );
}
