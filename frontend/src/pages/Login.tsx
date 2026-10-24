import { FormEvent, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { post } from "../api";
import { ErrorBox } from "../components/ui";

export function Login() {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const qc = useQueryClient();
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await post("/api/auth/login", { username, password });
      await qc.invalidateQueries();
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="flex min-h-full items-center justify-center p-4">
      <form onSubmit={submit} className="card w-full max-w-sm space-y-4 p-6">
        <div className="flex items-center gap-2">
          <img src="/favicon.svg" className="h-8 w-8" alt="" />
          <h1 className="text-lg font-semibold">Media Integrity Scanner</h1>
        </div>
        <div>
          <label className="label" htmlFor="u">Benutzername</label>
          <input id="u" className="input" value={username} onChange={(e) => setUsername(e.target.value)} autoComplete="username" required />
        </div>
        <div>
          <label className="label" htmlFor="p">Passwort</label>
          <input id="p" type="password" className="input" value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="current-password" required />
        </div>
        <ErrorBox error={error} />
        <button className="btn-primary w-full justify-center" disabled={busy}>Anmelden</button>
      </form>
    </div>
  );
}
