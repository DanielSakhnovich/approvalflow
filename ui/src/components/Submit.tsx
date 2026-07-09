import { useEffect, useState } from "react";
import { api } from "../api";

interface Fixture {
  id: string;
  [k: string]: unknown;
}

const SAMPLE = JSON.stringify(
  {
    id: "INV-1001",
    submitter: "dana.cohen@northwind.example",
    department: "engineering-2026Q2",
    vendor: "Bistro 19",
    vendorKnown: true,
    invoiceNumber: "NW-INV-7781",
    currency: "USD",
    category: "meals",
    attendees: 1,
    lineItems: [{ description: "Team lunch", quantity: 1, unitPrice: 38.89 }],
    taxAmount: 3.11,
    total: 42.0,
    receiptPresent: true,
    date: "2026-05-12",
    notes: "Solo working lunch.",
  },
  null,
  2,
);

export function Submit({ onSubmitted }: { onSubmitted: (id: string) => void }) {
  const [fixtures, setFixtures] = useState<Fixture[]>([]);
  const [json, setJson] = useState(SAMPLE);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);

  // The shipped fixtures are served as a static file by the gateway; use them
  // to prefill the form so a demo can drive any decision path in one click.
  useEffect(() => {
    fetch("/sample-invoices.json")
      .then((r) => (r.ok ? r.json() : Promise.reject()))
      .then((d) => setFixtures(d.fixtures ?? []))
      .catch(() => setFixtures([]));
  }, []);

  const loadFixture = (id: string) => {
    const f = fixtures.find((x) => x.id === id);
    if (!f) return;
    const { expected, scenario, ...invoice } = f as Record<string, unknown>;
    void expected;
    void scenario;
    setJson(JSON.stringify(invoice, null, 2));
  };

  const submit = async () => {
    setErr("");
    setBusy(true);
    try {
      const invoice = JSON.parse(json);
      const res = await api.submit(invoice);
      onSubmitted(res.trackingId);
    } catch (e) {
      setErr(String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="panel">
      <h2>Submit an invoice</h2>
      <p className="muted">
        Returns immediately with a tracking id; the decision arrives asynchronously.
      </p>
      {fixtures.length > 0 && (
        <>
          <label>Prefill from a shipped fixture</label>
          <select onChange={(e) => loadFixture(e.target.value)} defaultValue="">
            <option value="" disabled>
              choose a fixture…
            </option>
            {fixtures.map((f) => (
              <option key={f.id} value={f.id}>
                {f.id}
              </option>
            ))}
          </select>
        </>
      )}
      <label>Invoice JSON</label>
      <textarea value={json} onChange={(e) => setJson(e.target.value)} />
      <div style={{ marginTop: 12 }}>
        <button className="primary" onClick={submit} disabled={busy}>
          {busy ? "Submitting…" : "Submit"}
        </button>
      </div>
      {err && <div className="err">{err}</div>}
    </div>
  );
}
