import { useState, type FormEvent } from "react";
import { useApp } from "../app/AppContext";
import { SignInForm } from "../screens/SignIn";
import { renew } from "../lib/session";
import { Alert, Field, TextInput, errorText } from "./ui";

/**
 * Online sign-in followed by the PIN: enrols the device again (new secret and expiry, same data key).
 * Used when the offline period is over (TST-10) and after the server refused the device (401).
 */
export function RenewPanel({ onDone, reason }: { onDone: () => void; reason: string }) {
  const { signedInOnline, profile, refresh } = useApp();
  const [pin, setPin] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await renew(pin);
      setPin("");
      await refresh();
      onDone();
    } catch (err) {
      setError(errorText(err));
      await refresh();
    } finally {
      setBusy(false);
    }
  }

  if (!signedInOnline || (profile && signedInOnline.name !== profile.user)) {
    return (
      <div className="space-y-3">
        <Alert kind="warn">{reason}</Alert>
        {signedInOnline && profile ? (
          <Alert kind="error">
            You are signed in as {signedInOnline.name}, but this device belongs to {profile.full_name || profile.user}. Sign in as that user.
          </Alert>
        ) : null}
        <SignInForm onDone={() => void refresh()} hint={profile ? `Sign in as ${profile.full_name || profile.user}.` : undefined} />
      </div>
    );
  }
  return (
    <form onSubmit={submit} className="card space-y-4">
      <Alert kind="ok">Signed in online as {signedInOnline.full_name || signedInOnline.name}. Enter your PIN to renew this device.</Alert>
      <Field label="PIN" htmlFor="renew-pin">
        <TextInput id="renew-pin" type="password" inputMode="numeric" autoComplete="current-password" value={pin} onChange={(e) => setPin(e.target.value.replace(/\D/g, ""))} />
      </Field>
      {error ? <Alert kind="error">{error}</Alert> : null}
      <button type="submit" className="btn-primary w-full" disabled={busy || !pin}>
        {busy ? "Renewing…" : "Renew and unlock"}
      </button>
    </form>
  );
}
