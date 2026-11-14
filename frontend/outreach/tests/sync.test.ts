import type { Keys } from "../src/lib/crypto";
import { getMeta } from "../src/lib/db";
import { DAY_MS, addRecord, allRows, counts, orderForSend, purge } from "../src/lib/outbox";
import {
  DEFAULT_SETTINGS,
  buildCommunityActivity,
  buildEnvelope,
  type AnyEnvelope,
  type PushArgs,
  type RecordResult,
} from "../src/lib/protocol";
import { runSync, type SyncContext } from "../src/lib/sync";
import { freshDevice, HttpError, mockServer, testKeys, type Call } from "./helpers";

const SESSION = "KLB-OUT-2026-00001";
const DEVICE = "device-1";
const PUSH = "onerc_healthcare.outreach.sync.push";
const STATUS = "onerc_healthcare.outreach.sync.status";

let keys: Keys;
let clock: number;

function ctx(overrides: Partial<SyncContext> = {}): SyncContext {
  return {
    keys,
    session: SESSION,
    deviceId: DEVICE,
    settings: { ...DEFAULT_SETTINGS, max_batch: 2, purge_after_days: 7 },
    credentials: { grant: "GRANT-1", secret: "s3cr3t" },
    now: () => clock,
    ...overrides,
  };
}

async function activity(n: number): Promise<AnyEnvelope> {
  const env = buildEnvelope("community.activity", buildCommunityActivity({ activity_type: "Health talk", activity_date: "2026-11-20", topic: `talk ${n}`, participants_female: n }), {
    session: SESSION,
    device_id: DEVICE,
    captured_at: "2026-11-20 10:00:00",
  });
  await addRecord(keys, env, `talk ${n}`, null, clock);
  return env;
}

async function registerAndVisit(visitFirst = false): Promise<{ reg: AnyEnvelope; visit: AnyEnvelope }> {
  const reg = buildEnvelope(
    "patient.register",
    { first_name: "Amina", middle_name: null, last_name: "Hassan", sex: "Female", dob: null, estimated_age_years: 30, estimated_age_months: null, mobile: null, residence: null, allergies: null, is_walk_in: 1, identifiers: [] },
    { session: SESSION, device_id: DEVICE, captured_at: "2026-11-20 10:00:00" },
  );
  const visit = buildEnvelope(
    "visit.capture",
    {
      patient: null,
      patient_ref: reg.client_uuid,
      department: "Outpatient",
      consultation: { presenting_complaint: null, history: null, examination_findings: null, management_plan: null, no_diagnosis_reason: "Review only", diagnoses: [] },
      lab_requests: [],
      prescriptions: [],
      outcome: "Discharged home",
    },
    { session: SESSION, device_id: DEVICE, captured_at: "2026-11-20 10:05:00", depends_on: [reg.client_uuid] },
  );
  if (visitFirst) {
    await addRecord(keys, visit, "visit");
    await addRecord(keys, reg, "reg");
  } else {
    await addRecord(keys, reg, "reg");
    await addRecord(keys, visit, "visit");
  }
  return { reg, visit };
}

const ok = (uuid: string, result: RecordResult["result"] = "accepted", extra: Partial<RecordResult> = {}): RecordResult => ({
  client_uuid: uuid,
  result,
  doctype: result === "accepted" || result === "duplicate" ? "Community Activity Record" : null,
  name: result === "accepted" || result === "duplicate" ? `CAR-${uuid.slice(0, 4)}` : null,
  conflict: null,
  error: result === "failed" ? "Something is wrong" : null,
  ...extra,
});

const pushed = (calls: Call[]): PushArgs[] => calls.filter((c) => c.path === PUSH).map((c) => c.body as PushArgs);
const statusOf = async () => Object.fromEntries((await allRows()).map((r) => [r.client_uuid, r.status]));

describe("sync engine (OUT-14..17, OUT-24, NFR-11)", () => {
  beforeEach(async () => {
    await freshDevice();
    keys = await testKeys();
    clock = Date.parse("2026-11-20T08:00:00Z");
  });

  it("sends envelopes with the device grant header, no cookie, in batches of max_batch with a batch_id", async () => {
    const envs = [await activity(1), await activity(2), await activity(3)];
    const { calls } = mockServer({ [PUSH]: (c) => ({ batch: "B", results: (c.body as PushArgs).records.map((r) => ok(r.client_uuid)) }) });
    const report = await runSync(ctx(), "manual");
    expect(report.stoppedBy).toBeNull();
    const pushes = calls.filter((c) => c.path === PUSH);
    expect(pushes).toHaveLength(2); // 2 + 1
    for (const c of pushes) {
      expect(c.method).toBe("POST");
      expect(c.headers.Authorization).toBe("krcs-outreach GRANT-1:s3cr3t");
      expect(c.credentials).toBe("omit");
      expect(c.headers["X-Frappe-CSRF-Token"]).toBeUndefined();
      const body = c.body as PushArgs;
      expect(Object.keys(body).sort()).toEqual(["batch_id", "device_id", "records", "session"]);
      expect(body.batch_id).toMatch(/^[0-9a-f-]{36}$/);
      expect(body.records.length).toBeLessThanOrEqual(2);
    }
    expect(pushed(calls).flatMap((b) => b.records)).toEqual(envs);
    expect((await counts()).accepted).toBe(3);
  });

  it("keeps every record until a final result; failed/held/conflict stay unsynced", async () => {
    const [a, b, c, d] = [await activity(1), await activity(2), await activity(3), await activity(4)];
    const answers: Record<string, RecordResult["result"]> = { [a.client_uuid]: "accepted", [b.client_uuid]: "failed", [c.client_uuid]: "held", [d.client_uuid]: "conflict" };
    mockServer({
      [PUSH]: (call) => ({ batch: "B", results: (call.body as PushArgs).records.map((r) => ok(r.client_uuid, answers[r.client_uuid])) }),
      [STATUS]: (call) => ({ results: (JSON.parse(call.query.get("uuids")!) as string[]).map((u) => ok(u, answers[u])) }),
    });
    await runSync(ctx(), "manual");
    expect(await statusOf()).toEqual({ [a.client_uuid]: "accepted", [b.client_uuid]: "failed", [c.client_uuid]: "held", [d.client_uuid]: "conflict" });
    const rows = await allRows();
    expect(rows).toHaveLength(4);
    expect(rows.filter((r) => r.final).map((r) => r.client_uuid)).toEqual([a.client_uuid]);
    expect((await counts()).unsynced).toBe(3);
  });

  it("resumes after an interrupted sync: the next run sends only the unacknowledged records (OUT-16)", async () => {
    const envs = [];
    for (let i = 1; i <= 5; i++) envs.push(await activity(i));
    let pushCount = 0;
    const first = mockServer({
      [PUSH]: (c) => {
        pushCount += 1;
        if (pushCount === 2) throw "network"; // the connection drops on the second batch
        return { batch: "B", results: (c.body as PushArgs).records.map((r) => ok(r.client_uuid)) };
      },
    });
    const report = await runSync(ctx(), "online");
    expect(report.stoppedBy).toBe("network");
    expect(pushed(first.calls)).toHaveLength(2); // stopped: the third batch was never tried
    const s = await statusOf();
    expect([s[envs[0]!.client_uuid], s[envs[1]!.client_uuid]]).toEqual(["accepted", "accepted"]);
    expect(envs.slice(2).map((e) => s[e.client_uuid])).toEqual(["pending", "pending", "pending"]);
    const lostBatch = pushed(first.calls)[1]!;
    expect(await getMeta("inflight_batch")).toEqual({ batch_id: lostBatch.batch_id, uuids: lostBatch.records.map((r) => r.client_uuid) });

    const second = mockServer({ [PUSH]: (c) => ({ batch: "B", results: (c.body as PushArgs).records.map((r) => ok(r.client_uuid, "accepted")) }) });
    const again = await runSync(ctx(), "start");
    expect(again.stoppedBy).toBeNull();
    const sent = pushed(second.calls);
    expect(sent.flatMap((b) => b.records.map((r) => r.client_uuid))).toEqual(envs.slice(2).map((e) => e.client_uuid));
    expect(sent[0]!.batch_id).toBe(lostBatch.batch_id); // the unanswered batch is re-sent as-is
    expect(sent[1]!.batch_id).not.toBe(lostBatch.batch_id);
    expect((await counts()).unsynced).toBe(0);
    expect(await getMeta("inflight_batch")).toBeUndefined();
  });

  it("treats duplicate as final, and a held/conflict record resolved later (sync.status) as final", async () => {
    const [a, b] = [await activity(1), await activity(2)];
    let resolved = false;
    mockServer({
      [PUSH]: (c) => ({ batch: "B", results: (c.body as PushArgs).records.map((r) => (r.client_uuid === a.client_uuid ? ok(r.client_uuid, "duplicate") : ok(r.client_uuid, "conflict", { conflict: "SC-1" }))) }),
      [STATUS]: (c) => ({
        results: (JSON.parse(c.query.get("uuids")!) as string[]).map((u) => (resolved ? ok(u, "accepted", { discarded: true }) : ok(u, "conflict", { conflict: "SC-1" }))),
      }),
    });
    await runSync(ctx(), "manual");
    expect(await statusOf()).toEqual({ [a.client_uuid]: "duplicate", [b.client_uuid]: "conflict" });
    resolved = true;
    const report = await runSync(ctx(), "timer");
    expect(report.resolved).toBe(1);
    const rows = await allRows();
    expect(rows.map((r) => [r.status, r.final])).toEqual([
      ["duplicate", 1],
      ["resolved", 1],
    ]);
  });

  it("re-sends failed records but not held/conflict ones", async () => {
    const [a, b] = [await activity(1), await activity(2)];
    const first = mockServer({ [PUSH]: (c) => ({ batch: "B", results: (c.body as PushArgs).records.map((r) => ok(r.client_uuid, r.client_uuid === a.client_uuid ? "failed" : "held")) }), [STATUS]: (c) => ({ results: (JSON.parse(c.query.get("uuids")!) as string[]).map((u) => ok(u, "held")) }) });
    await runSync(ctx(), "manual");
    expect(pushed(first.calls)).toHaveLength(1);
    const second = mockServer({ [PUSH]: (c) => ({ batch: "B", results: (c.body as PushArgs).records.map((r) => ok(r.client_uuid)) }), [STATUS]: (c) => ({ results: (JSON.parse(c.query.get("uuids")!) as string[]).map((u) => ok(u, "held")) }) });
    await runSync(ctx(), "manual");
    expect(pushed(second.calls).flatMap((p) => p.records.map((r) => r.client_uuid))).toEqual([a.client_uuid]);
    expect(second.calls.filter((c) => c.path === STATUS).map((c) => JSON.parse(c.query.get("uuids")!))).toEqual([[b.client_uuid]]);
  });

  it("on 401 stops, keeps the records and asks for an online sign-in", async () => {
    await activity(1);
    const onAuthRequired = vi.fn();
    mockServer({ [PUSH]: () => new HttpError(401, { exc_type: "AuthenticationError" }) });
    const report = await runSync(ctx({ onAuthRequired }), "manual");
    expect(report.stoppedBy).toBe("auth");
    expect(onAuthRequired).toHaveBeenCalledTimes(1);
    expect((await allRows()).map((r) => r.status)).toEqual(["pending"]);
  });

  it("purges final records purge_after_days after the acknowledgement, never failed/held/conflict ones (OUT-24)", async () => {
    // only Date is faked: fake-indexeddb schedules its work with real timers
    vi.useFakeTimers({ toFake: ["Date"] });
    try {
      vi.setSystemTime(new Date("2026-11-20T08:00:00Z"));
      clock = Date.now();
      const [a, b, c] = [await activity(1), await activity(2), await activity(3)];
      mockServer({
        [PUSH]: (call) => ({ batch: "B", results: (call.body as PushArgs).records.map((r) => ok(r.client_uuid, r.client_uuid === a.client_uuid ? "accepted" : r.client_uuid === b.client_uuid ? "failed" : "held")) }),
        [STATUS]: (call) => ({ results: (JSON.parse(call.query.get("uuids")!) as string[]).map((u) => ok(u, "held")) }),
      });
      await runSync(ctx({ now: () => Date.now() }), "manual");
      expect((await allRows()).find((r) => r.client_uuid === a.client_uuid)!.acked_at).toBe(Date.now());

      vi.setSystemTime(Date.now() + 7 * DAY_MS - 1);
      expect(await purge(7)).toBe(0);
      expect(await allRows()).toHaveLength(3);

      vi.setSystemTime(Date.now() + 1);
      expect(await purge(7)).toBe(1);
      expect((await allRows()).map((r) => r.client_uuid).sort()).toEqual([b.client_uuid, c.client_uuid].sort());

      vi.setSystemTime(Date.now() + 60 * DAY_MS);
      expect(await purge(7)).toBe(0); // failed and held are never purged
      expect(await allRows()).toHaveLength(2);
    } finally {
      vi.useRealTimers();
    }
  });

  it("sends a registration before the visits that depend on it, even when captured later", async () => {
    const { reg, visit } = await registerAndVisit(true);
    const before = await allRows();
    expect(before.map((r) => r.client_uuid)).toEqual([visit.client_uuid, reg.client_uuid]); // seq order
    expect(orderForSend(before).map((r) => r.client_uuid)).toEqual([reg.client_uuid, visit.client_uuid]);

    const { calls } = mockServer({ [PUSH]: (c) => ({ batch: "B", results: (c.body as PushArgs).records.map((r) => ok(r.client_uuid)) }) });
    await runSync(ctx({ settings: { ...DEFAULT_SETTINGS, max_batch: 1 } }), "manual");
    expect(pushed(calls).map((b) => b.records[0]!.type)).toEqual(["patient.register", "visit.capture"]);
    expect(pushed(calls)[1]!.records[0]!.depends_on).toEqual([reg.client_uuid]);
  });

  it("does not send a visit whose registration failed in the same run", async () => {
    const { reg, visit } = await registerAndVisit();
    const { calls } = mockServer({ [PUSH]: (c) => ({ batch: "B", results: (c.body as PushArgs).records.map((r) => ok(r.client_uuid, r.type === "patient.register" ? "failed" : "accepted")) }) });
    await runSync(ctx({ settings: { ...DEFAULT_SETTINGS, max_batch: 1 } }), "manual");
    expect(pushed(calls)).toHaveLength(1);
    expect(await statusOf()).toEqual({ [reg.client_uuid]: "failed", [visit.client_uuid]: "pending" });
  });

  it("a server error on a whole batch stops without losing anything", async () => {
    await activity(1);
    mockServer({ [PUSH]: () => new HttpError(417, { _server_messages: JSON.stringify([JSON.stringify({ message: "Session KLB-OUT-2026-00001 is closed" })]) }) });
    const report = await runSync(ctx(), "manual");
    expect(report.stoppedBy).toBe("server");
    expect(report.error).toBe("Session KLB-OUT-2026-00001 is closed");
    expect((await allRows())[0]!.status).toBe("pending");
  });
});
