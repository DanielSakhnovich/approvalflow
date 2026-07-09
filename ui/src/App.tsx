import { useState } from "react";
import { Submit } from "./components/Submit";
import { StatusView } from "./components/StatusView";
import { ApproverQueue } from "./components/ApproverQueue";
import { Dashboard } from "./components/Dashboard";
import { Thresholds } from "./components/Thresholds";
import { Compliance } from "./components/Compliance";

type Tab = "submit" | "status" | "queue" | "dashboard" | "thresholds" | "compliance";

const TABS: { id: Tab; label: string }[] = [
  { id: "submit", label: "Submit" },
  { id: "status", label: "Status" },
  { id: "queue", label: "Approver queue" },
  { id: "dashboard", label: "Dashboard" },
  { id: "thresholds", label: "Thresholds" },
  { id: "compliance", label: "Compliance" },
];

export function App() {
  const [tab, setTab] = useState<Tab>("submit");
  // Lets Submit hand a tracking id to Status without a router.
  const [trackId, setTrackId] = useState("");

  const goStatus = (id: string) => {
    setTrackId(id);
    setTab("status");
  };

  return (
    <>
      <header>
        <h1>ApprovalFlow</h1>
        <span className="tag">invoice &amp; expense approval</span>
      </header>
      <nav>
        {TABS.map((t) => (
          <button
            key={t.id}
            className={tab === t.id ? "active" : ""}
            onClick={() => setTab(t.id)}
          >
            {t.label}
          </button>
        ))}
      </nav>
      <main>
        {tab === "submit" && <Submit onSubmitted={goStatus} />}
        {tab === "status" && <StatusView initialId={trackId} />}
        {tab === "queue" && <ApproverQueue />}
        {tab === "dashboard" && <Dashboard />}
        {tab === "thresholds" && <Thresholds />}
        {tab === "compliance" && <Compliance />}
      </main>
    </>
  );
}
