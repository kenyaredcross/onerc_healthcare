import { getMeta } from "../src/lib/db";
import { addRecord, listItems } from "../src/lib/outbox";
import { DEFAULT_SETTINGS, buildCommunityActivity, buildEnvelope, type OutreachSessionSummary } from "../src/lib/protocol";
import {
  ExpiredError,
  IdleLock,
  LockedOutError,
  PinError,
  WrongUserError,
  deviceCredentials,
  enrolNew,
  getDeviceId,
  getKeys,
  getProfile,
  lock,
  lockState,
  recoverKey,
  renew,
  requireKeys,
  setPin,
  signInOnline,
  unlock,
} from "../src/lib/session";
import { formatSiteTime } from "../src/lib/time";
import { HttpError, freshDevice, mockServer, type Call } from "./helpers";

const DAY = 86_400_000;
const SESSION: OutreachSessionSummary = {
  name: "KLB-OUT-2026-00001",
  title: "Village 3",
  facility: "F",
  facility_name: "Natukobenyo",
  start_date: "2026-11-20",
  end_date: "2026-11-21",
  catchment_area: "CA",
  status: "Planned",
  departments: ["Outpatient"],
};

function server(opts: { user?: string; expiresIn?: number } = {}) {
  const escrow: { key: string | null; secrets: string[]; enrols: Call[] } = { key: null, secrets: [], enrols: [] };
  let user = opts.user ?? "nurse@krcs.test";
  const mock = mockServer({
    login: (c) => {
      const b = c.body as { usr: string; pwd: string };
      if (b.pwd !== "pw") return new HttpError(401, { message: "Invalid Login. Try again." });
      user = b.usr;
      return { message: "Logged In", full_name: "Nurse" };
    },
    "onerc_healthcare.www.outreach.get_boot": () => ({
      site_name: "emr-test.localhost",
      csrf_token: "csrf-1",
      timezone: "Africa/Nairobi",
      user: { name: user, full_name: `Full ${user}` },
      sw_url: "/outreach/sw.js",
      build: null,
    }),
    "onerc_healthcare.outreach.devices.enrol": (c) => {
      escrow.enrols.push(c);
      const b = c.body as { data_key: string };
      escrow.key = b.data_key;
      const secret = `s${escrow.secrets.length + 1}`;
      escrow.secrets.push(secret);
      return {
        grant: "GRANT-1",
        secret,
        expires_at: formatSiteTime(Date.now() + (opts.expiresIn ?? 7 * DAY)),
        user: { name: user, full_name: `Full ${user}` },
        settings: { ...DEFAULT_SETTINGS, max_pin_attempts: 3 },
      };
    },
    "onerc_healthcare.outreach.devices.recover_key": (c) => {
      expect(c.query.get("grant")).toBe("GRANT-1");
      return { data_key: escrow.key };
    },
  });
  return { escrow, calls: mock.calls };
}

async function enrolled(opts: { expiresIn?: number } = {}) {
  const s = server(opts);
  await signInOnline("nurse@krcs.test", "pw");
  await enrolNew(SESSION, "Tablet 1");
  await setPin("123456");
  return s;
}

async function addActivity(): Promise<string> {
  const env = buildEnvelope("community.activity", buildCommunityActivity({ activity_type: "Health talk", activity_date: "2026-11-20", participants_male: 2 }), {
    session: SESSION.name,
    device_id: await getDeviceId(),
    captured_at: "2026-11-20 10:00:00",
  });
  await addRecord(requireKeys(), env, "Health talk");
  return env.client_uuid;
}

describe("device session (OUT-07/08, NFR-18)", () => {
  beforeEach(freshDevice);

  it("signs in online, enrols with the data key escrowed, stores the secret encrypted and asks for a PIN", async () => {
    const s = await enrolled();
    const enrol = s.escrow.enrols[0]!;
    expect(enrol.method).toBe("POST");
    expect(enrol.headers["X-Frappe-CSRF-Token"]).toBe("csrf-1");
    expect(Object.keys(enrol.body as object).sort()).toEqual(["data_key", "device_id", "device_label", "session"]);
    expect((enrol.body as { device_id: string }).device_id).toBe(await getDeviceId());
    expect(atob((enrol.body as { data_key: string }).data_key)).toHaveLength(32);

    const meta = JSON.stringify(await getMeta("secure:secret"));
    expect(meta).not.toContain('"s1"');
    expect(await deviceCredentials()).toEqual({ grant: "GRANT-1", secret: "s1" });
    expect(await lockState()).toBe("unlocked");
    lock();
    expect(await lockState()).toBe("locked");
    await unlock("123456");
    expect(await lockState()).toBe("unlocked");
  });

  it("refuses a PIN that is too short or not numeric", async () => {
    server();
    await signInOnline("nurse@krcs.test", "pw");
    await enrolNew(SESSION, "Tablet 1");
    expect(await lockState()).toBe("needs-pin");
    await expect(setPin("12345")).rejects.toThrow(/at least 6/);
    await expect(setPin("12a456")).rejects.toThrow(/digits/);
  });

  it("discards the wrapped key after max_pin_attempts wrong PINs, then recovers it from escrow after an online sign-in", async () => {
    await enrolled();
    const uuid = await addActivity();
    lock();
    // the online session ended: recovery must ask for a new sign-in
    const { resetMemory } = await import("../src/lib/session");
    resetMemory();

    await expect(unlock("000000")).rejects.toBeInstanceOf(PinError);
    await expect(unlock("111111")).rejects.toThrow(/1 attempt left/);
    await expect(unlock("222222")).rejects.toBeInstanceOf(LockedOutError);
    expect(await getMeta("wrapped_key")).toBeUndefined();
    expect(await lockState()).toBe("locked-out");
    // even the right PIN no longer works
    await expect(unlock("123456")).rejects.toBeInstanceOf(LockedOutError);

    await expect(recoverKey()).rejects.toThrow(/Sign in online/);
    await signInOnline("nurse@krcs.test", "pw");
    await recoverKey();
    expect(await lockState()).toBe("needs-pin");
    await setPin("975310");
    lock();
    await unlock("975310");
    // the records written before the lockout are still readable: nothing was lost (NFR-11)
    const items = await listItems(requireKeys());
    expect(items.map((i) => i.row.client_uuid)).toEqual([uuid]);
  });

  it("refuses recovery for another user", async () => {
    await enrolled();
    lock();
    for (const pin of ["1", "2", "3"]) await unlock(pin.repeat(6)).catch(() => undefined);
    expect(await lockState()).toBe("locked-out");
    await signInOnline("other@krcs.test", "pw");
    await expect(recoverKey()).rejects.toBeInstanceOf(WrongUserError);
  });

  it("TST-10: after expires_at the PIN no longer unlocks; an online sign-in re-enrols with the same data key", async () => {
    const s = await enrolled({ expiresIn: 2 * DAY });
    const uuid = await addActivity();
    lock();
    const later = Date.now() + 3 * DAY;
    expect(await lockState(later)).toBe("expired");
    await expect(unlock("123456", later)).rejects.toBeInstanceOf(ExpiredError);

    const { resetMemory } = await import("../src/lib/session");
    resetMemory(); // a new app start: no online session
    await expect(renew("123456")).rejects.toThrow(/Sign in online/);
    await signInOnline("nurse@krcs.test", "pw");
    const firstKey = s.escrow.enrols[0]!.body as { data_key: string };
    await renew("123456");
    const second = s.escrow.enrols[1]!.body as { data_key: string; session: string };
    expect(second.data_key).toBe(firstKey.data_key); // same data key
    expect(second.session).toBe(SESSION.name);
    expect(await deviceCredentials()).toEqual({ grant: "GRANT-1", secret: "s2" }); // new secret
    expect((await getProfile())!.expires_epoch).toBeGreaterThan(Date.now() + DAY); // new expiry from the server
    expect(await lockState()).toBe("unlocked");
    expect((await listItems(requireKeys()))[0]!.row.client_uuid).toBe(uuid);
  });

  it("expiry also wins over an app left unlocked", async () => {
    await enrolled({ expiresIn: DAY });
    expect(await lockState()).toBe("unlocked");
    expect(await lockState(Date.now() + 2 * DAY)).toBe("expired");
  });

  it("a wrong PIN during renewal counts as an attempt", async () => {
    await enrolled({ expiresIn: DAY });
    await expect(renew("999999")).rejects.toBeInstanceOf(PinError);
    expect(await getMeta("pin_attempts")).toBe(1);
  });

  it("refuses to switch session while records are not synced", async () => {
    await enrolled();
    await addActivity();
    await expect(enrolNew({ ...SESSION, name: "KLB-OUT-2026-00002" }, "Tablet 1")).rejects.toThrow(/not synced/);
  });

  it("locks after 15 minutes without activity", () => {
    vi.useFakeTimers();
    try {
      const onIdle = vi.fn();
      const idle = new IdleLock(onIdle);
      idle.touch();
      vi.advanceTimersByTime(14 * 60_000);
      idle.touch(); // activity resets the timer
      vi.advanceTimersByTime(14 * 60_000);
      expect(onIdle).not.toHaveBeenCalled();
      vi.advanceTimersByTime(60_001);
      expect(onIdle).toHaveBeenCalledTimes(1);
      idle.stop();
    } finally {
      vi.useRealTimers();
    }
  });

  it("keeps keys only in memory: lock() forgets them", async () => {
    await enrolled();
    expect(getKeys()).not.toBeNull();
    lock();
    expect(getKeys()).toBeNull();
  });
});
