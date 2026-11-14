/**
 * Fix round 1 (protocol §6): F1 closed sessions, F2 correcting failed records, F3 prescribed quantity,
 * F4 base_values chaining, F7 app shell without CSRF token or user. Expectations are hand-computed.
 */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import type { Keys } from "../src/lib/crypto";
import { db, getMeta } from "../src/lib/db";
import { allRows, getRow, readSecret, requeueFailed, NotCorrectableError } from "../src/lib/outbox";
import { fromPackage, getPatient, patientKey, putPatient, type LocalPatient } from "../src/lib/patients";
import {
  DEFAULT_SETTINGS,
  buildEnvelope,
  prescribedQuantity,
  type AnyEnvelope,
  type Envelope,
  type OutreachSessionSummary,
  type PackagePatient,
  type PatientUpdatePayload,
  type PushArgs,
  type RecordResult,
  type ReferenceData,
} from "../src/lib/protocol";
import {
  activityFormFromPayload,
  correctActivity,
  correctPatientUpdate,
  correctRegistration,
  finishVisit,
  recordActivity,
  registerFormFromPayload,
  registerPatient,
  updatePatient,
} from "../src/lib/records";
import { buildIcdIndex } from "../src/lib/icd";
import type { LoadedReference } from "../src/lib/reference";
import {
  enrolNew,
  getDeviceId,
  getKeys,
  getProfile,
  lockState,
  renew,
  resetDevice,
  setPin,
  signInOnline,
  startNewSession,
} from "../src/lib/session";
import { getSessionStatus, setSessionStatus } from "../src/lib/sessionStatus";
import { runSync, type SyncContext } from "../src/lib/sync";
import { formatSiteTime } from "../src/lib/time";
import {
  buildVisitCapture,
  countDrafts,
  draftFromVisit,
  newDraft,
  saveDraft,
  validatePrescriptions,
  withDefaultQuantity,
  type DraftPrescription,
} from "../src/lib/visit";
import { HttpError, freshDevice, mockServer, testKeys, type Call } from "./helpers";

const SESSION = "KLB-OUT-2026-00001";
const PUSH = "onerc_healthcare.outreach.sync.push";
const ENROL = "onerc_healthcare.outreach.devices.enrol";
const MY_SESSIONS = "onerc_healthcare.outreach.devices.my_sessions";
const GET_BOOT = "onerc_healthcare.www.outreach.get_boot";
const DAY = 86_400_000;

const pushed = (calls: Call[]): PushArgs[] => calls.filter((c) => c.path === PUSH).map((c) => c.body as PushArgs);
const result = (uuid: string, r: RecordResult["result"], extra: Partial<RecordResult> = {}): RecordResult => ({
  client_uuid: uuid,
  result: r,
  doctype: null,
  name: null,
  conflict: null,
  error: null,
  ...extra,
});

function pkg(extra: Partial<PackagePatient> = {}): PackagePatient {
  return {
    name: "KLB-PAT-2026-00012",
    modified: "2026-11-10 08:01:02.123456",
    first_name: "Amina",
    middle_name: null,
    last_name: "Hassan",
    sex: "Female",
    dob: "1990-05-01",
    dob_estimated: 0,
    mobile: null,
    residence: "Village 3",
    allergies: null,
    identifiers: [],
    recent_visits: [],
    active_prescriptions: [],
    ...extra,
  };
}

async function loadPatient(keys: Keys, extra: Partial<PackagePatient> = {}): Promise<LocalPatient> {
  const p = pkg(extra);
  const local = fromPackage(await patientKey(keys, { name: p.name, client_uuid: null }), p);
  await putPatient(keys, local);
  return local;
}

// ===================================================================================================
// F1: a Closed session keeps syncing; renew after 401; start a new session once everything is final
// ===================================================================================================

describe("F1 closed and cancelled sessions", () => {
  const SUMMARY: OutreachSessionSummary = {
    name: SESSION,
    title: "Village 3",
    facility: "F",
    facility_name: "Natukobenyo",
    start_date: "2026-11-20",
    end_date: "2026-11-21",
    catchment_area: "CA",
    status: "Active",
    departments: ["Outpatient"],
  };

  /** A server whose session can be Active, Closed or Cancelled, applying protocol §6 F1. */
  function server() {
    const state = {
      status: "Active" as "Active" | "Closed" | "Cancelled",
      secret: "",
      secrets: [] as string[],
      enrols: [] as Call[],
      grantValid: true,
    };
    const mock = mockServer({
      login: () => ({ message: "Logged In" }),
      [GET_BOOT]: () => ({ site_name: "emr-test.localhost", csrf_token: "csrf", timezone: "Africa/Nairobi", user: { name: "nurse@krcs.test", full_name: "Nurse" }, sw_url: "/outreach/sw.js", build: null }),
      [MY_SESSIONS]: () => (state.status === "Active" ? [{ ...SUMMARY, status: "Active" }] : []),
      [ENROL]: (c) => {
        state.enrols.push(c);
        if (state.status === "Cancelled") return new HttpError(417, { exc_type: "ValidationError", _server_messages: JSON.stringify([JSON.stringify({ message: `Outreach session ${SESSION} is Cancelled; it no longer accepts records.` })]) });
        state.secret = `s${state.secrets.length + 1}`;
        state.secrets.push(state.secret);
        state.grantValid = true;
        return { grant: "GRANT-1", secret: state.secret, expires_at: formatSiteTime(Date.now() + 7 * DAY), user: { name: "nurse@krcs.test", full_name: "Nurse" }, settings: DEFAULT_SETTINGS };
      },
      [PUSH]: (c) => {
        if (!state.grantValid || c.headers.Authorization !== `krcs-outreach GRANT-1:${state.secret}`) {
          return new HttpError(401, { exc_type: "AuthenticationError", message: "Invalid device credentials. Sign in online." });
        }
        if (state.status === "Cancelled") {
          return new HttpError(417, { exc_type: "ValidationError", _server_messages: JSON.stringify([JSON.stringify({ message: `Outreach session ${SESSION} is Cancelled; it no longer accepts records.` })]) });
        }
        return { batch: "B", results: (c.body as PushArgs).records.map((r) => result(r.client_uuid, "accepted", { doctype: "Community Activity Record", name: "CAR-1" })) };
      },
    });
    return { state, calls: mock.calls };
  }

  async function enrolledDevice() {
    const srv = server();
    await signInOnline("nurse@krcs.test", "pw");
    await enrolNew(SUMMARY, "Tablet");
    await setPin("123456");
    return srv;
  }

  async function syncCtx(): Promise<SyncContext> {
    const profile = (await getProfile())!;
    const keys = getKeys()!;
    const { deviceCredentials, markAuthRequired } = await import("../src/lib/session");
    return { keys, session: profile.session, deviceId: await getDeviceId(), settings: profile.settings, credentials: await deviceCredentials(), onAuthRequired: markAuthRequired };
  }

  async function talk(n: number): Promise<string> {
    const profile = (await getProfile())!;
    return recordActivity(getKeys()!, { session: profile.session, deviceId: await getDeviceId() }, { activity_type: "Health talk", activity_date: "2026-11-20", topic: `t${n}`, participants_female: 3 });
  }

  beforeEach(freshDevice);

  it("a Closed session still accepts the device's records; a 401 is answered by an online sign-in that renews the grant", async () => {
    const srv = await enrolledDevice();
    const a = await talk(1);
    const b = await talk(2);

    // the facility closes the session; my_sessions no longer lists it
    srv.state.status = "Closed";
    await signInOnline("nurse@krcs.test", "pw");
    expect((await getSessionStatus())?.status).toBe("Ended");

    // the grant still syncs (F1): the device learns the session is Closed, not Cancelled
    const r1 = await runSync(await syncCtx(), "manual");
    expect(r1.stoppedBy).toBeNull();
    expect((await getRow(a))!.status).toBe("accepted");
    expect((await getSessionStatus())?.status).toBe("Closed");

    // the grant expires after the session closed: 401, records kept, online sign-in asked for
    const c = await talk(3);
    srv.state.grantValid = false;
    const r2 = await runSync(await syncCtx(), "manual");
    expect(r2.stoppedBy).toBe("auth");
    expect(await getMeta("auth_required")).toBe(true);
    expect((await getRow(c))!.status).toBe("pending");

    // renew: enrol again for the same device and user (the server renews on a Closed session)
    await renew("123456");
    expect(srv.state.enrols).toHaveLength(2);
    const [first, second] = srv.state.enrols.map((e) => e.body as { session: string; device_id: string; data_key: string });
    expect(second!.session).toBe(SESSION);
    expect(second!.device_id).toBe(first!.device_id);
    expect(second!.data_key).toBe(first!.data_key); // same data key: the records stay readable
    expect(await getMeta("auth_required")).toBeUndefined();

    const r3 = await runSync(await syncCtx(), "manual");
    expect(r3.stoppedBy).toBeNull();
    expect(pushed(srv.calls).at(-1)!.records.map((r) => r.client_uuid)).toEqual([c]);
    expect((await allRows()).every((r) => r.final)).toBe(true);
    expect(b).toBeTruthy();

    // every record final: start a new session; no patient data is kept, the device id is
    const deviceId = await getDeviceId();
    await putPatient(getKeys()!, fromPackage("k1", pkg()));
    await startNewSession();
    expect(await lockState()).toBe("new");
    expect(await getProfile()).toBeUndefined();
    expect(await (await db()).count("patients")).toBe(0);
    expect(await (await db()).count("outbox")).toBe(0);
    expect(await getSessionStatus()).toBeNull();
    expect(await getDeviceId()).toBe(deviceId);
  });

  it("a Cancelled session: records are kept, never deleted, and a new session cannot be started", async () => {
    const srv = await enrolledDevice();
    const a = await talk(1);
    srv.state.status = "Cancelled";
    const r = await runSync(await syncCtx(), "manual");
    expect(r.stoppedBy).toBe("server");
    expect((await getSessionStatus())?.status).toBe("Cancelled");
    expect((await getRow(a))!.status).toBe("pending");

    await expect(startNewSession()).rejects.toThrow(/no final result yet/);
    await expect(resetDevice()).rejects.toThrow(/not synced/);
    expect(await getRow(a)).toBeTruthy();

    // a renewal refused because the session is cancelled is remembered too
    srv.state.grantValid = false;
    await signInOnline("nurse@krcs.test", "pw");
    await expect(renew("123456")).rejects.toThrow(/Cancelled/);
    expect((await getSessionStatus())?.status).toBe("Cancelled");
    // a later "not listed" never downgrades Cancelled
    await setSessionStatus("Ended");
    expect((await getSessionStatus())?.status).toBe("Cancelled");
  });

  it("start a new session is refused while a visit is unfinished", async () => {
    await enrolledDevice();
    await saveDraft(getKeys()!, newDraft({ key: "k", name: "P", client_uuid: null, display: "A" }, "Outpatient"));
    await expect(startNewSession()).rejects.toThrow(/unfinished visit/);
    expect(await getProfile()).toBeTruthy();
  });
});

// ===================================================================================================
// F2: a failed record is corrected and re-sent with the same client_uuid; its dependants follow
// ===================================================================================================

describe("F2 correcting a failed record", () => {
  let keys: Keys;
  let clock: number;
  const ctx = (): SyncContext => ({
    keys,
    session: SESSION,
    deviceId: "dev-1",
    settings: { ...DEFAULT_SETTINGS, max_batch: 1 },
    credentials: { grant: "G", secret: "S" },
    now: () => clock,
  });

  beforeEach(async () => {
    await freshDevice();
    keys = await testKeys();
    clock = Date.parse("2026-11-20T08:00:00Z");
  });

  it("a refused registration is corrected, re-sent with the same uuid and new payload; the visit held behind it follows", async () => {
    const rctx = { session: SESSION, deviceId: "dev-1", now: clock };
    const patient = await registerPatient(keys, rctx, {
      first_name: "Fatuma",
      last_name: "Ali",
      sex: "Female",
      dob_mode: "estimated",
      age_years: "25",
      is_walk_in: false,
      identifiers: [{ identifier_type: "UNHCR ID", identifier_value: "bad" }],
    });
    const regUuid = patient.client_uuid!;
    const draft = newDraft({ key: patient.key, name: null, client_uuid: regUuid, display: "Fatuma Ali" }, "Outpatient");
    draft.consultation.no_diagnosis_reason = "Review";
    const visitUuid = await finishVisit(keys, rctx, draft, REF);

    // the server refuses the identifier (twice: one history line, counted)
    const answers: Record<string, (env: AnyEnvelope) => RecordResult> = {
      [regUuid]: (env) =>
        (env.payload as { identifiers: { identifier_value: string }[] }).identifiers[0]?.identifier_value === "bad"
          ? result(regUuid, "failed", { error: "bad is not a valid UNHCR ID." })
          : result(regUuid, "accepted", { doctype: "Patient", name: "KLB-PAT-2026-00099" }),
      [visitUuid]: () => result(visitUuid, "accepted", { doctype: "Patient Visit", name: "KLB-PV-2026-00031" }),
    };
    const { calls } = mockServer({ [PUSH]: (c) => ({ batch: "B", results: (c.body as PushArgs).records.map((r) => answers[r.client_uuid]!(r)) }) });
    await runSync(ctx(), "manual");
    clock += 60_000;
    await runSync(ctx(), "manual");
    expect((await getRow(regUuid))!.status).toBe("failed");
    expect((await getRow(visitUuid))!.status).toBe("pending"); // held back behind the failed registration
    expect(pushed(calls).flatMap((b) => b.records.map((r) => r.client_uuid))).toEqual([regUuid, regUuid]);
    const failedSecret = await readSecret(keys, (await getRow(regUuid))!);
    expect(failedSecret.history).toEqual([{ kind: "failed", at: Date.parse("2026-11-20T08:00:00Z"), last_at: clock, count: 2, error: "bad is not a valid UNHCR ID." }]);

    // the form opens prefilled from the stored payload; the user fixes the identifier
    const form = registerFormFromPayload(failedSecret.envelope.payload as Envelope<"patient.register">["payload"]);
    expect(form).toMatchObject({ first_name: "Fatuma", last_name: "Ali", dob_mode: "estimated", age_years: "25", is_walk_in: false });
    form.identifiers = [{ identifier_type: "UNHCR ID", identifier_value: "123-12345678" }];
    form.first_name = "Fatuma ";
    await correctRegistration(keys, regUuid, form, clock);

    const row = (await getRow(regUuid))!;
    expect(row.status).toBe("pending");
    const fixed = await readSecret(keys, row);
    expect(fixed.envelope.client_uuid).toBe(regUuid);
    expect(fixed.envelope.captured_at).toBe(failedSecret.envelope.captured_at);
    expect(fixed.history!.map((h) => h.kind)).toEqual(["failed", "corrected"]);
    expect((await getPatient(keys, patient.key))!.identifiers).toEqual([{ identifier_type: "UNHCR ID", identifier_value: "123-12345678" }]);

    // next sync: the registration (same uuid) then the visit that waited for it
    await runSync(ctx(), "manual");
    const last = pushed(calls).slice(2).flatMap((b) => b.records);
    expect(last.map((r) => r.client_uuid)).toEqual([regUuid, visitUuid]);
    expect((last[0]!.payload as { identifiers: unknown[] }).identifiers).toEqual([{ identifier_type: "UNHCR ID", identifier_value: "123-12345678" }]);
    expect((await getRow(regUuid))!.status).toBe("accepted");
    expect((await getRow(visitUuid))!.status).toBe("accepted");
    expect((await getPatient(keys, patient.key))!.name).toBe("KLB-PAT-2026-00099");
  });

  it("a refused visit reopens prefilled in the stepper and is re-queued under its uuid with its patient_ref", async () => {
    const reg = buildEnvelope("patient.register", registerPayload(), { session: SESSION, device_id: "dev-1", captured_at: "2026-11-20 10:00:00" });
    const draft = newDraft({ key: "pk", name: null, client_uuid: reg.client_uuid, display: "Amina Hassan" }, "Outpatient", clock);
    draft.started_at = "2026-11-20 10:05:00";
    draft.consultation.diagnoses = [{ code: "CV-1F40", code_value: "1F40", title: "Malaria", case_type: "New", is_primary: true }];
    draft.labs = [{ line_id: "l1", template: "Malaria Rapid Diagnostic Test", poc: true, result: "Negative", resulted_at: "2026-11-20 10:10:00" }];
    draft.prescriptions = [rx({ dispensed: true, quantity: "6", dispensed_at: "2026-11-20 10:20:00" })];
    const uuid = await finishVisit(keys, { session: SESSION, deviceId: "dev-1" }, draft, REF);
    await failRecord(uuid, "Paracetamol 500mg is out of stock.");

    const env = (await readSecret(keys, (await getRow(uuid))!)).envelope as Envelope<"visit.capture">;
    const fix = draftFromVisit(env, "pk", "Amina Hassan", REF, clock);
    expect(fix.correcting).toBe(uuid);
    expect(fix.started_at).toBe("2026-11-20 10:05:00");
    expect(fix.consultation.diagnoses[0]).toMatchObject({ code: "CV-1F40", code_value: "1F40", title: "Malaria due to Plasmodium falciparum", is_primary: true });
    expect(fix.prescriptions[0]).toMatchObject({ medication: "Paracetamol", label: "Paracetamol 500mg", dispensed: true, quantity: "6" });
    // round trip: the prefilled draft rebuilds the stored payload exactly
    expect(buildVisitCapture(fix)).toEqual(env.payload);

    fix.prescriptions = [{ ...fix.prescriptions[0]!, dispensed: false, quantity: "", dispensed_at: null }];
    await saveDraft(keys, fix);
    expect(await finishVisit(keys, { session: SESSION, deviceId: "dev-1" }, fix, REF)).toBe(uuid);
    const rows = await allRows();
    expect(rows).toHaveLength(1);
    const after = await readSecret(keys, rows[0]!);
    expect(rows[0]!.status).toBe("pending");
    expect(after.envelope.depends_on).toEqual([reg.client_uuid]);
    expect((after.envelope.payload as Envelope<"visit.capture">["payload"]).patient_ref).toBe(reg.client_uuid);
    expect("dispensed" in (after.envelope.payload as Envelope<"visit.capture">["payload"]).prescriptions[0]!).toBe(false);
    expect(await countDrafts()).toBe(0);
  });

  it("activities and demographic edits are corrected the same way; only failed records can be", async () => {
    const uuid = await recordActivity(keys, { session: SESSION, deviceId: "dev-1" }, { activity_type: "Health talk", activity_date: "2026-11-20", topic: "Hygiene", participants_male: 2 });
    await expect(correctActivity(keys, uuid, { activity_type: "Health talk", activity_date: "2026-11-20" })).rejects.toBeInstanceOf(NotCorrectableError);
    await failRecord(uuid, "Unknown activity type.");
    const form = activityFormFromPayload((await readSecret(keys, (await getRow(uuid))!)).envelope.payload as Envelope<"community.activity">["payload"]);
    expect(form).toMatchObject({ topic: "Hygiene", participants_male: "2", participants_female: "0" });
    await correctActivity(keys, uuid, { ...form, activity_type: "Home visit" });
    expect(((await readSecret(keys, (await getRow(uuid))!)).envelope.payload as { activity_type: string }).activity_type).toBe("Home visit");

    const p = await loadPatient(keys);
    await updatePatient(keys, { session: SESSION, deviceId: "dev-1" }, p, { mobile: "07x" }, []);
    const upd = (await allRows()).at(-1)!.client_uuid;
    await failRecord(upd, "Enter a valid phone number.");
    await correctPatientUpdate(keys, upd, { mobile: "0712345678" }, []);
    const payload = (await readSecret(keys, (await getRow(upd))!)).envelope.payload as PatientUpdatePayload;
    expect(payload).toEqual({ patient: p.name, base_modified: p.modified, changes: { mobile: "0712345678" }, base_values: { mobile: null }, add_identifiers: [] });
    expect((await getPatient(keys, p.key))!.mobile).toBe("0712345678");
    // a record that is not failed is never re-queued
    await expect(requeueFailed(keys, upd, payload)).rejects.toBeInstanceOf(NotCorrectableError);
  });

  async function failRecord(uuid: string, error: string) {
    mockServer({ [PUSH]: (c) => ({ batch: "B", results: (c.body as PushArgs).records.map((r) => result(r.client_uuid, r.client_uuid === uuid ? "failed" : "accepted", { error: r.client_uuid === uuid ? error : null })) }) });
    await runSync({ ...ctx(), settings: { ...DEFAULT_SETTINGS } }, "manual");
    expect((await getRow(uuid))!.status).toBe("failed");
  }
});

// ===================================================================================================
// F3: prescribed quantity
// ===================================================================================================

const DATA: ReferenceData = {
  icd11: [["CV-1F40", "1F40", "Malaria due to Plasmodium falciparum", 1]],
  medications: [{ name: "Paracetamol", generic_name: "Paracetamol", strength: "500mg", dosage_form: "Tablet", default_dosage: "TID", default_period: "3 Day" }],
  dosages: ["OD", "BD", "TID", "Half daily"],
  periods: ["1 Day", "3 Day", "5 Day"],
  dosage_forms: ["Tablet"],
  lab_templates: [{ name: "Malaria Rapid Diagnostic Test", abbr: "MRDT", point_of_care: 1, data_type: "Select", options: ["Negative", "Positive - P. falciparum"], unit: null }],
  departments: ["Outpatient"],
  identifier_types: [{ name: "UNHCR ID", is_primary: 1, pattern: "^[0-9]{3}-[0-9]{8}$", facility_generated: 0 }],
  residences: [],
  triage_levels: ["Non-urgent"],
  community_activity_types: ["Health talk", "Home visit"],
  outcomes: ["Discharged home", "Died"],
  genders: ["Female", "Male"],
  dosage_units: { OD: 1, BD: 2, TID: 3, "Half daily": 0.5 },
  period_days: { "1 Day": 1, "3 Day": 3, "5 Day": 5 },
};
const icdIndex = buildIcdIndex(DATA.icd11);
const REF: LoadedReference = { ...DATA, icdIndex, icdByName: new Map(icdIndex.map((e) => [e.name, e])) };

function rx(extra: Partial<DraftPrescription> = {}): DraftPrescription {
  return { line_id: "p1", medication: "Paracetamol", label: "Paracetamol 500mg", dosage: "TID", period: "3 Day", dosage_form: "Tablet", comment: "", dispensed: false, quantity: "", dispensed_at: null, ...extra };
}

function registerPayload() {
  return { first_name: "Amina", middle_name: null, last_name: "Hassan", sex: "Female", dob: "1990-05-01", estimated_age_years: null, estimated_age_months: null, mobile: null, residence: null, allergies: null, is_walk_in: 1 as const, identifiers: [] };
}

describe("F3 prescribed quantity", () => {
  it("is dosage_units x period_days, at least 1; no limit without the reference keys", () => {
    expect(prescribedQuantity(DATA, "TID", "3 Day")).toBe(9);
    expect(prescribedQuantity(DATA, "BD", "5 Day")).toBe(10);
    expect(prescribedQuantity(DATA, "Half daily", "1 Day")).toBe(1); // 0.5, at least 1
    expect(prescribedQuantity(DATA, "Half daily", "5 Day")).toBe(2.5);
    expect(prescribedQuantity(DATA, null, "5 Day")).toBe(5); // daily interval: one dose a day
    expect(prescribedQuantity(DATA, "BD", null)).toBe(2);
    expect(prescribedQuantity(DATA, null, null)).toBe(1);
    expect(prescribedQuantity(DATA, "QID", "3 Day")).toBeNull(); // unknown to the package: no limit
    expect(prescribedQuantity({}, "TID", "3 Day")).toBeNull(); // an older server
  });

  it("defaults the dispensed quantity to it, follows dosage/period changes, keeps a typed quantity", () => {
    const on = withDefaultQuantity(rx(), rx({ dispensed: true }), DATA);
    expect(on.quantity).toBe("9");
    const longer = withDefaultQuantity(on, { ...on, period: "5 Day" }, DATA);
    expect(longer.quantity).toBe("15");
    const typed = withDefaultQuantity(longer, { ...longer, quantity: "4" }, DATA);
    expect(typed.quantity).toBe("4");
    expect(withDefaultQuantity(typed, { ...typed, dosage: "BD" }, DATA).quantity).toBe("4");
    expect(withDefaultQuantity(rx(), rx({ dispensed: true }), {}).quantity).toBe(""); // no reference keys
  });

  it("refuses dispensing more than prescribed; no limit with an older server", () => {
    expect(validatePrescriptions([rx({ dispensed: true, quantity: "9" })], DATA)).toEqual({});
    expect(validatePrescriptions([rx({ dispensed: true, quantity: "10" })], DATA)).toEqual({ p1: "Paracetamol 500mg: at most the prescribed quantity (9) can be dispensed." });
    expect(validatePrescriptions([rx({ dispensed: true, quantity: "4" })], DATA)).toEqual({}); // partial dispense
    expect(validatePrescriptions([rx({ dispensed: true, quantity: "100" })], {})).toEqual({});
  });
});

// ===================================================================================================
// F4: base_values chaining across two edits
// ===================================================================================================

describe("F4 patient.update base_values", () => {
  let keys: Keys;
  beforeEach(async () => {
    await freshDevice();
    keys = await testKeys();
  });

  /** The server rule of protocol §6 F4 on one patient row. */
  function patientServer(start: Record<string, string | null>, modified: string) {
    const row: Record<string, string | null> = { ...start };
    let version = modified;
    let n = 0;
    const conflicts: string[] = [];
    const mock = mockServer({
      [PUSH]: (c) => ({
        batch: "B",
        results: (c.body as PushArgs).records.map((env) => {
          const p = env.payload as PatientUpdatePayload;
          if (p.base_modified > version) return result(env.client_uuid, "failed", { error: "base_modified is later" });
          const clash = Object.entries(p.changes).filter(([k, v]) => {
            if (!p.base_values) return p.base_modified < version && row[k] !== v;
            return row[k] !== (p.base_values[k as keyof typeof p.base_values] ?? null) && row[k] !== v;
          });
          if (clash.length) {
            conflicts.push(env.client_uuid);
            return result(env.client_uuid, "conflict", { conflict: "SC-1" });
          }
          Object.assign(row, p.changes);
          version = `2026-11-20 0${++n}:00:00.000000`;
          return result(env.client_uuid, "accepted", { doctype: "Patient", name: p.patient, modified: version });
        }),
      }),
    });
    return { row, conflicts, calls: mock.calls, version: () => version };
  }

  it("chains two pending edits (base_values = values after the first) and adopts the returned modified", async () => {
    const p = await loadPatient(keys);
    const rctx = { session: SESSION, deviceId: "dev-1" };
    await updatePatient(keys, rctx, p, { mobile: "0711000001" }, []);
    const afterFirst = (await getPatient(keys, p.key))!;
    await updatePatient(keys, rctx, afterFirst, { mobile: "0711000002", residence: "Village 4" }, []);

    const [r1, r2] = await allRows();
    const e1 = (await readSecret(keys, r1!)).envelope as Envelope<"patient.update">;
    const e2 = (await readSecret(keys, r2!)).envelope as Envelope<"patient.update">;
    expect(e1.payload.base_values).toEqual({ mobile: null });
    expect(e1.depends_on).toEqual([]);
    expect(e2.payload.base_modified).toBe("2026-11-10 08:01:02.123456"); // unchanged
    expect(e2.payload.base_values).toEqual({ mobile: "0711000001", residence: "Village 3" });
    expect(e2.depends_on).toEqual([e1.client_uuid]); // applied after the first

    const srv = patientServer({ mobile: null, residence: "Village 3" }, "2026-11-10 08:01:02.123456");
    const ctx: SyncContext = { keys, session: SESSION, deviceId: "dev-1", settings: DEFAULT_SETTINGS, credentials: { grant: "G", secret: "S" } };
    await runSync(ctx, "manual");
    expect(srv.conflicts).toEqual([]); // no false conflict on the second edit
    expect(srv.row).toEqual({ mobile: "0711000002", residence: "Village 4" });
    const local = (await getPatient(keys, p.key))!;
    expect(local.modified).toBe("2026-11-20 02:00:00.000000");
    expect(local.pending_updates).toBe(0);

    // a third edit after sync starts from the server's new version
    await updatePatient(keys, rctx, local, { mobile: "0711000003" }, []);
    const e3 = (await readSecret(keys, (await allRows()).at(-1)!)).envelope as Envelope<"patient.update">;
    expect(e3.payload.base_modified).toBe("2026-11-20 02:00:00.000000");
    expect(e3.payload.base_values).toEqual({ mobile: "0711000002" });
    expect(e3.depends_on).toEqual([]);
    await runSync(ctx, "manual");
    expect(srv.conflicts).toEqual([]);
    expect(srv.row.mobile).toBe("0711000003");
  });

  it("still raises a conflict when the facility changed the same field", async () => {
    const p = await loadPatient(keys);
    await updatePatient(keys, { session: SESSION, deviceId: "dev-1" }, p, { mobile: "0711000001" }, []);
    const srv = patientServer({ mobile: "0799999999", residence: "Village 3" }, "2026-11-15 08:00:00.000000");
    await runSync({ keys, session: SESSION, deviceId: "dev-1", settings: DEFAULT_SETTINGS, credentials: { grant: "G", secret: "S" } }, "manual");
    expect(srv.conflicts).toHaveLength(1);
  });
});

// ===================================================================================================
// F7: the cached app shell carries no CSRF token and no user
// ===================================================================================================

describe("F7 app shell", () => {
  const www = fileURLToPath(new URL("../../../onerc_healthcare/www/", import.meta.url));
  const py = readFileSync(`${www}outreach.py`, "utf-8");
  const html = readFileSync(`${www}outreach.html`, "utf-8");

  function pyFunction(name: string): string {
    const start = py.indexOf(`def ${name}(`);
    expect(start).toBeGreaterThanOrEqual(0);
    const rest = py.slice(start + 1);
    const next = rest.search(/\n(?:def |@)/);
    return next >= 0 ? rest.slice(0, next) : rest;
  }

  it("the page boot holds only site, timezone and asset info; get_boot adds the token and user", () => {
    const page = pyFunction("_page_boot");
    expect(page).not.toMatch(/csrf/i);
    expect(page).not.toMatch(/"user"|session\.user|get_fullname/);
    expect(page).toMatch(/"site_name"/);
    expect(page).toMatch(/"timezone"/);
    expect(page).toMatch(/"sw_url"/);
    expect(pyFunction("get_context")).toMatch(/boot_json = _script_json\(_page_boot\(\)\)/);
    expect(pyFunction("get_context")).not.toMatch(/_script_json\(_boot\(\)\)/);
    expect(pyFunction("_boot")).toMatch(/csrf_token/);
    // the template embeds nothing else from the server
    expect(html).not.toMatch(/csrf|frappe\.session|\{\{\s*user/i);
    expect([...html.matchAll(/\{\{\s*([a-z_.]+)/g)].map((m) => m[1]).sort()).toEqual(["asset_base", "asset_base", "asset_base", "boot_json", "href", "src", "sw_source"]);
  });
});
