import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import { fetchBoot } from "../lib/api";
import { getMeta } from "../lib/db";
import { counts as outboxCounts, type OutboxCounts } from "../lib/outbox";
import { countPatients } from "../lib/patients";
import { clearReferenceCache, loadReference, type LoadedReference } from "../lib/reference";
import {
  IdleLock,
  adoptOnlineUser,
  checkSessionStatus,
  deviceCredentials,
  getDeviceId,
  getKeys,
  getProfile,
  isAuthRequired,
  lock as lockSession,
  lockState,
  markAuthRequired,
  onSessionChange,
  onlineUser,
  type LockState,
  type Profile,
} from "../lib/session";
import { getSyncState, isSyncRunning, onSyncChange, runSync, type SyncReport, type SyncState, type SyncTrigger } from "../lib/sync";
import { getSessionStatus, onSessionStatusChange, type SessionStatusInfo } from "../lib/sessionStatus";
import { setSiteTimezone } from "../lib/time";
import { countDrafts } from "../lib/visit";
import { pageBoot } from "../boot";

const SYNC_INTERVAL_MS = 5 * 60 * 1000;

export interface AppState {
  ready: boolean;
  lock: LockState;
  profile: Profile | null;
  deviceId: string;
  online: boolean;
  signedInOnline: { name: string; full_name: string } | null;
  reference: LoadedReference | null;
  hasPackage: boolean;
  counts: OutboxCounts | null;
  patients: number;
  drafts: number;
  syncState: SyncState | null;
  syncing: boolean;
  authRequired: boolean;
  /** What the device knows about its session on the server (F1); null when nothing is known. */
  sessionStatus: SessionStatusInfo | null;
  refresh: () => Promise<void>;
  sync: (trigger: SyncTrigger) => Promise<SyncReport | null>;
  lockNow: () => void;
}

const Ctx = createContext<AppState | null>(null);

export function useApp(): AppState {
  const v = useContext(Ctx);
  if (!v) throw new Error("useApp outside AppProvider");
  return v;
}

export function AppProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<Omit<AppState, "refresh" | "sync" | "lockNow">>({
    ready: false,
    lock: "new",
    profile: null,
    deviceId: "",
    online: typeof navigator === "undefined" ? true : navigator.onLine,
    signedInOnline: null,
    reference: null,
    hasPackage: false,
    counts: null,
    patients: 0,
    drafts: 0,
    syncState: null,
    syncing: false,
    authRequired: false,
    sessionStatus: null,
  });

  const refresh = useCallback(async () => {
    const [lock, profile, deviceId, counts, patients, drafts, syncState, authRequired, packageAt, sessionStatus] = await Promise.all([
      lockState(),
      getProfile(),
      getDeviceId(),
      outboxCounts(),
      countPatients(),
      countDrafts(),
      getSyncState(),
      isAuthRequired(),
      getMeta<number>("package_at"),
      getSessionStatus(),
    ]);
    const keys = getKeys();
    const reference = lock === "unlocked" && keys ? await loadReference(keys) : null;
    if (lock !== "unlocked") clearReferenceCache();
    setState((s) => ({
      ...s,
      ready: true,
      lock,
      profile: profile ?? null,
      deviceId,
      signedInOnline: onlineUser(),
      reference,
      hasPackage: Boolean(packageAt) && reference !== null,
      counts,
      patients,
      drafts,
      syncState,
      syncing: isSyncRunning(),
      authRequired,
      sessionStatus,
    }));
  }, []);

  const sync = useCallback(
    async (trigger: SyncTrigger): Promise<SyncReport | null> => {
      const keys = getKeys();
      const profile = await getProfile();
      if (!keys || !profile || (await lockState()) !== "unlocked") return null;
      if (trigger !== "manual" && typeof navigator !== "undefined" && !navigator.onLine) return null;
      const report = await runSync(
        {
          keys,
          session: profile.session,
          deviceId: await getDeviceId(),
          settings: profile.settings,
          credentials: await deviceCredentials(),
          onAuthRequired: markAuthRequired,
        },
        trigger,
      );
      await refresh();
      return report;
    },
    [refresh],
  );

  const lockNow = useCallback(() => {
    lockSession();
  }, []);

  // boot. The cached page (www/outreach.py) carries only the site timezone and asset info, never the
  // CSRF token or the user (F7): those come from get_boot when the server can be reached.
  useEffect(() => {
    let cancelled = false;
    const boot = pageBoot();
    if (boot?.timezone) setSiteTimezone(boot.timezone);
    void refresh();
    fetchBoot()
      .then(async (b) => {
        if (cancelled) return;
        setSiteTimezone(b.timezone);
        adoptOnlineUser(b.user, b.csrf_token);
        if (b.user && b.user.name !== "Guest") await checkSessionStatus();
      })
      .catch(() => undefined) // offline: the app works without it
      .finally(() => {
        if (!cancelled) void refresh();
      });
    return () => {
      cancelled = true;
    };
  }, [refresh]);

  useEffect(() => {
    const offSession = onSessionChange(() => void refresh());
    const offStatus = onSessionStatusChange(() => void refresh());
    const offSync = onSyncChange(() => setState((s) => ({ ...s, syncing: isSyncRunning() })));
    return () => {
      offSession();
      offStatus();
      offSync();
    };
  }, [refresh]);

  // online / offline
  useEffect(() => {
    const on = () => {
      setState((s) => ({ ...s, online: true }));
      void sync("online");
    };
    const off = () => setState((s) => ({ ...s, online: false }));
    window.addEventListener("online", on);
    window.addEventListener("offline", off);
    return () => {
      window.removeEventListener("online", on);
      window.removeEventListener("offline", off);
    };
  }, [sync]);

  // sync when the app becomes usable, and every few minutes while it is
  useEffect(() => {
    if (state.lock !== "unlocked" || !state.hasPackage) return;
    void sync("start");
    const t = setInterval(() => void sync("timer"), SYNC_INTERVAL_MS);
    return () => clearInterval(t);
  }, [state.lock, state.hasPackage, sync]);

  // idle lock after 15 minutes without activity (NFR-18)
  useEffect(() => {
    if (state.lock !== "unlocked") return;
    const idle = new IdleLock(() => lockSession());
    const events = ["pointerdown", "keydown", "touchstart", "scroll"] as const;
    events.forEach((e) => window.addEventListener(e, idle.touch, { passive: true }));
    idle.touch();
    return () => {
      idle.stop();
      events.forEach((e) => window.removeEventListener(e, idle.touch));
    };
  }, [state.lock]);

  const value = useMemo<AppState>(() => ({ ...state, refresh, sync, lockNow }), [state, refresh, sync, lockNow]);
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}
