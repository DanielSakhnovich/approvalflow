import { useState, type FormEvent } from "react";
import { api } from "../api";

// Demo-only credentials mirroring afcommon.auth.SEED_USERS -- convenience
// buttons so a demo can switch roles without memorizing passwords. The form
// below still accepts arbitrary username/password for the same endpoint.
const DEMO_USERS: { username: string; password: string; role: string }[] = [
  { username: "alice", password: "alice-demo-pw", role: "submitter" },
  { username: "revi", password: "revi-demo-pw", role: "approver" },
  { username: "admin", password: "admin-demo-pw", role: "admin" },
];

export function Login({ onLoggedIn }: { onLoggedIn: () => void }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);

  const login = async (user: string, pass: string) => {
    setErr("");
    setBusy(true);
    try {
      await api.login(user, pass);
      onLoggedIn();
    } catch (e) {
      setErr(String(e));
    } finally {
      setBusy(false);
    }
  };

  const submit = (e: FormEvent) => {
    e.preventDefault();
    void login(username, password);
  };

  return (
    <div className="panel">
      <h2>Sign in</h2>
      <p className="muted">
        Demo credentials — pick a seeded role, or enter a username/password.
      </p>
      <div className="row">
        {DEMO_USERS.map((u) => (
          <button
            key={u.username}
            type="button"
            className="sm"
            disabled={busy}
            onClick={() => login(u.username, u.password)}
          >
            {u.role} ({u.username})
          </button>
        ))}
      </div>
      <form onSubmit={submit}>
        <label>Username</label>
        <input
          value={username}
          onChange={(e) => setUsername(e.target.value)}
          autoComplete="username"
        />
        <label>Password</label>
        <input
          type="password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          autoComplete="current-password"
        />
        <div style={{ marginTop: 12 }}>
          <button className="primary" type="submit" disabled={busy}>
            {busy ? "Signing in…" : "Sign in"}
          </button>
        </div>
      </form>
      {err && <div className="err">{err}</div>}
    </div>
  );
}
