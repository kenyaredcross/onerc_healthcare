import { useState, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import { useApp } from "../app/AppContext";
import { Alert, Field, Page, TextInput, errorText } from "../components/ui";
import { NetworkError } from "../lib/api";
import { signInOnline } from "../lib/session";

export function SignInForm({ onDone, hint }: { onDone: (user: { name: string; full_name: string }) => void; hint?: string }) {
  const { online } = useApp();
  const [usr, setUsr] = useState("");
  const [pwd, setPwd] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const user = await signInOnline(usr.trim(), pwd);
      setPwd("");
      onDone(user);
    } catch (err) {
      setError(err instanceof NetworkError ? "No connection to the server. Connect to the internet and try again." : errorText(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <form onSubmit={submit} className="card space-y-4" autoComplete="on">
      {hint ? <Alert>{hint}</Alert> : null}
      {!online ? <Alert kind="warn">You are offline. Online sign-in needs a connection.</Alert> : null}
      <Field label="Email or username" htmlFor="usr">
        <TextInput id="usr" name="username" autoComplete="username" required value={usr} onChange={(e) => setUsr(e.target.value)} />
      </Field>
      <Field label="Password" htmlFor="pwd">
        <TextInput
          id="pwd"
          name="password"
          type="password"
          autoComplete="current-password"
          required
          value={pwd}
          onChange={(e) => setPwd(e.target.value)}
        />
      </Field>
      {error ? <Alert kind="error">{error}</Alert> : null}
      <button type="submit" className="btn-primary w-full" disabled={busy || !usr || !pwd}>
        {busy ? "Signing in…" : "Sign in"}
      </button>
    </form>
  );
}

export default function SignInScreen() {
  const navigate = useNavigate();
  const { refresh } = useApp();
  return (
    <Page title="KRCS Outreach">
      <p className="text-slate-700">Sign in with your EMR account to prepare this device for an outreach session.</p>
      <SignInForm
        onDone={async () => {
          await refresh();
          navigate("/sessions", { replace: true });
        }}
      />
    </Page>
  );
}
