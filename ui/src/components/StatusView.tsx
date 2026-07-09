import { useEffect, useState } from "react";
import { api, InvoiceView } from "../api";

export function StatusView({ initialId }: { initialId: string }) {
  const [id, setId] = useState(initialId);
  const [view, setView] = useState<InvoiceView | null>(null);
  const [showTrail, setShowTrail] = useState(false);
  const [err, setErr] = useState("");

  const load = async (withTrail: boolean) => {
    setErr("");
    try {
      setView(await api.status(id, withTrail));
    } catch (e) {
      setView(null);
      setErr(String(e));
    }
  };

  // Auto-load and poll while an id is present and not yet terminal.
  useEffect(() => {
    if (!initialId) return;
    setId(initialId);
    void load(false);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initialId]);

  useEffect(() => {
    if (!view) return;
    const terminal = ["paid", "rejected", "duplicate", "payment_failed"].includes(
      view.status,
    );
    if (terminal) return;
    const t = setInterval(() => void load(showTrail), 2000);
    return () => clearInterval(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [view?.status, showTrail]);

  return (
    <div className="panel">
      <h2>Track a submission</h2>
      <div className="row">
        <input
          value={id}
          onChange={(e) => setId(e.target.value)}
          placeholder="tracking id (inv_…)"
        />
        <button className="primary" style={{ flex: "0 0 auto" }} onClick={() => load(showTrail)}>
          Look up
        </button>
      </div>
      {err && <div className="err">{err}</div>}
      {view && (
        <div style={{ marginTop: 16 }}>
          <div className="stat">
            <span>Status</span>
            <span className={`pill ${view.status}`}>{view.status}</span>
          </div>
          {view.route && (
            <div className="stat">
              <span>Route</span>
              <span className={`pill ${view.route}`}>{view.route}</span>
            </div>
          )}
          <div className="stat">
            <span>Reasoning</span>
            <span style={{ textAlign: "right", maxWidth: "60%" }}>{view.reasoning || "—"}</span>
          </div>
          <div className="stat">
            <span>Decided by</span>
            <span className="mono">{view.decidedBy || "—"}</span>
          </div>
          <div className="stat">
            <span>Updated</span>
            <span className="mono">{view.updatedAt}</span>
          </div>

          <div style={{ marginTop: 12 }}>
            <button
              className="sm"
              onClick={() => {
                const next = !showTrail;
                setShowTrail(next);
                void load(next);
              }}
            >
              {showTrail ? "Hide" : "Show"} audit trail
            </button>
          </div>

          {showTrail && view.trail && (
            <ul className="timeline mono" style={{ marginTop: 12 }}>
              {view.trail.map((e) => (
                <li key={e.event_id}>
                  <span className="ok-badge">{e.event_type}</span> — {e.occurred_at}
                </li>
              ))}
              {view.trail.length === 0 && <li className="muted">no trail yet</li>}
            </ul>
          )}
        </div>
      )}
    </div>
  );
}
