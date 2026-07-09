import { useEffect, useState } from "react";
import { api, Compliance as C } from "../api";

export function Compliance() {
  const [c, setC] = useState<C | null>(null);
  const [err, setErr] = useState("");

  useEffect(() => {
    api.compliance().then(setC).catch((e) => setErr(String(e)));
  }, []);

  return (
    <div className="panel">
      <h2>Ceiling compliance (F10)</h2>
      <p className="muted">
        Prove the system never auto-approved above the configured limit. An empty violations
        list over a non-zero checked count is the proof.
      </p>
      {err && <div className="err">{err}</div>}
      {c && (
        <>
          <div className="stat">
            <span>Auto-approvals checked</span>
            <b>{c.autoApprovalsChecked}</b>
          </div>
          <div className="stat">
            <span>Ceiling violations</span>
            {c.violations.length === 0 ? (
              <span className="ok-badge">0 — compliant ✓</span>
            ) : (
              <span className="bad-badge">{c.violations.length} — VIOLATION</span>
            )}
          </div>
        </>
      )}
    </div>
  );
}
