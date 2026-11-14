/**
 * Correct a record the server refused (`failed`) and send it again with the SAME client_uuid (fix round 1,
 * F2). The form of the record's type opens prefilled from the stored payload and is validated the same
 * way as a new record. Visits open in the visit stepper (a correction draft).
 */
import { useEffect, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { useApp } from "../app/AppContext";
import { Alert, Page, Spinner, errorText } from "../components/ui";
import type { Envelope } from "../lib/protocol";
import { readAck, type OutboxSecret } from "../lib/outbox";
import { getPatient, type LocalPatient } from "../lib/patients";
import {
  activityFormFromPayload,
  correctActivity,
  correctPatientUpdate,
  correctRegistration,
  correctionBase,
  loadFailed,
  registerFormFromPayload,
} from "../lib/records";
import { requireKeys } from "../lib/session";
import { draftFromVisit, loadDraft, saveDraft } from "../lib/visit";
import { ActivityEditor } from "./Activity";
import { EditPatientEditor, editFormOf, type EditForm } from "./EditPatient";
import { RegisterEditor } from "./Register";

interface Loaded {
  secret: OutboxSecret;
  error: string | null;
  patient: LocalPatient | null;
}

function Intro({ error }: { error: string | null }) {
  return (
    <Alert kind="warn">
      The server refused this record{error ? `: ${error}` : "."} Correct it and save: it is sent again as the same record, so it is never saved
      twice.
    </Alert>
  );
}

export default function FixRecordScreen() {
  const { uuid = "" } = useParams();
  const navigate = useNavigate();
  const { reference, refresh } = useApp();
  const [loaded, setLoaded] = useState<Loaded | null | undefined>(undefined);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      const keys = requireKeys();
      const { secret } = await loadFailed(keys, uuid);
      const ack = await readAck(keys, uuid);
      const patient = secret.patient_key ? await getPatient(keys, secret.patient_key) : null;
      if (secret.envelope.type === "visit.capture") {
        // the visit stepper edits a correction draft; an earlier unfinished correction is reopened as it was
        const env = secret.envelope as Envelope<"visit.capture">;
        const id = `fix-${uuid}`;
        if (!(await loadDraft(keys, id))) {
          if (!reference) throw new Error("The package is not loaded.");
          const display = secret.summary.replace(/^Visit: /, "").replace(/ \([^)]*\)$/, "");
          await saveDraft(keys, draftFromVisit(env, secret.patient_key ?? "", display, reference));
          await refresh();
        }
        if (!cancelled) navigate(`/visit/${encodeURIComponent(id)}`, { replace: true });
        return;
      }
      if (!cancelled) setLoaded({ secret, error: ack?.error ?? null, patient });
    })().catch((e) => {
      if (!cancelled) {
        setError(errorText(e));
        setLoaded(null);
      }
    });
    return () => {
      cancelled = true;
    };
  }, [uuid, reference, navigate, refresh]);

  if (loaded === undefined) return <Spinner />;
  if (!loaded || !reference) {
    return (
      <Page title="Correct record" back="/sync">
        <Alert kind="error">{error ?? "This record cannot be corrected."}</Alert>
      </Page>
    );
  }
  const { secret, patient } = loaded;
  const keys = requireKeys();
  const intro = <Intro error={loaded.error} />;

  switch (secret.envelope.type) {
    case "patient.register": {
      const env = secret.envelope;
      return (
        <RegisterEditor
          title="Correct registration"
          back="/sync"
          submitLabel="Save and send again"
          intro={intro}
          initial={registerFormFromPayload(env.payload)}
          excludeKey={secret.patient_key ?? undefined}
          onSave={(form) => correctRegistration(keys, uuid, form)}
        />
      );
    }
    case "community.activity": {
      const env = secret.envelope;
      return (
        <ActivityEditor
          title="Correct activity"
          back="/sync"
          done="/sync"
          submitLabel="Save and send again"
          intro={intro}
          initial={activityFormFromPayload(env.payload)}
          onSave={(form) => correctActivity(keys, uuid, form)}
        />
      );
    }
    case "patient.update": {
      if (!patient) {
        return (
          <Page title="Correct demographics" back="/sync">
            <Alert kind="error">The patient of this record is no longer on the device. Tell the HIS Officer; the record is kept.</Alert>
          </Page>
        );
      }
      const p = secret.envelope.payload;
      const form: EditForm = editFormOf({ ...patient, ...(Object.fromEntries(Object.entries(p.changes).filter(([k]) => !k.startsWith("estimated_age"))) as Partial<LocalPatient>) });
      if (p.changes.estimated_age_years !== undefined || p.changes.estimated_age_months !== undefined) {
        form.dob_mode = "estimated";
        form.age_years = String(p.changes.estimated_age_years ?? "");
        form.age_months = String(p.changes.estimated_age_months ?? "");
      } else if (p.changes.dob) {
        form.dob_mode = "exact";
        form.dob = String(p.changes.dob);
      }
      return (
        <EditPatientEditor
          title="Correct demographics"
          patient={patient}
          base={correctionBase(patient, p)}
          initialForm={form}
          initialIds={p.add_identifiers.map((i) => ({ ...i }))}
          intro={intro}
          done="/sync"
          onSave={async (changes, ids) => {
            await correctPatientUpdate(keys, uuid, changes, ids);
          }}
        />
      );
    }
    default:
      return <Spinner />;
  }
}
