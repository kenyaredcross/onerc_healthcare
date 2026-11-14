import { useState, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import { useApp } from "../app/AppContext";
import { Alert, Field, Page, TextInput, errorText } from "../components/ui";
import { setPin } from "../lib/session";

export default function SetPinScreen() {
  const navigate = useNavigate();
  const { profile, refresh } = useApp();
  const min = profile?.settings.pin_min_length ?? 6;
  const [pin, setPinValue] = useState("");
  const [again, setAgain] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (pin !== again) {
      setError("The two PINs do not match.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await setPin(pin);
      await refresh();
      navigate("/", { replace: true });
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Page title="Set your PIN">
      <Alert>
        The PIN unlocks this app offline and protects the patient records on this device. Use at least {min} digits and do not share it.
      </Alert>
      <form onSubmit={submit} className="card space-y-4">
        <Field label={`New PIN (${min}+ digits)`} htmlFor="pin">
          <TextInput id="pin" type="password" inputMode="numeric" autoComplete="new-password" value={pin} onChange={(e) => setPinValue(e.target.value.replace(/\D/g, ""))} />
        </Field>
        <Field label="Repeat the PIN" htmlFor="pin2">
          <TextInput id="pin2" type="password" inputMode="numeric" autoComplete="new-password" value={again} onChange={(e) => setAgain(e.target.value.replace(/\D/g, ""))} />
        </Field>
        {error ? <Alert kind="error">{error}</Alert> : null}
        <button type="submit" className="btn-primary w-full" disabled={busy || pin.length < min}>
          {busy ? "Protecting…" : "Set PIN"}
        </button>
      </form>
    </Page>
  );
}
