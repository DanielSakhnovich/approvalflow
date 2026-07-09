import { useEffect, useState } from "react";
import { Submit } from "./components/Submit";
import { StatusView } from "./components/StatusView";
import { ApproverQueue } from "./components/ApproverQueue";
import { Dashboard } from "./components/Dashboard";
import { Thresholds } from "./components/Thresholds";
import { Compliance } from "./components/Compliance";
import { Login } from "./components/Login";
import { api, getToken, setUnauthorizedListener } from "./api";

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
  // N1: gate the whole app behind a token. Restored from localStorage on
  // load (api.ts reads it at module init), so a refresh doesn't force a
  // re-login.
  const [loggedIn, setLoggedIn] = useState(() => Boolean(getToken()));

  // A 401 from anywhere in the app (an expired/invalid token) drops back to
  // Login; registered here so it's not tied to whichever component happened
  // to make the failing request.
  useEffect(() => {
    setUnauthorizedListener(() => setLoggedIn(false));
    return () => setUnauthorizedListener(null);
  }, []);

  const goStatus = (id: string) => {
    setTrackId(id);
    setTab("status");
  };

  const logout = () => {
    api.logout();
    setLoggedIn(false);
  };

  if (!loggedIn) {
    return (
      <>
        <header>
          <h1>ApprovalFlow</h1>
          <span className="tag">invoice &amp; expense approval</span>
        </header>
        <main>
          <Login onLoggedIn={() => setLoggedIn(true)} />
        </main>
      </>
    );
  }

  return (
    <>
      <header>
        <h1>ApprovalFlow</h1>
        <span className="tag">invoice &amp; expense approval</span>
        <button className="sm" style={{ marginLeft: "auto" }} onClick={logout}>
          Sign out
        </button>
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
