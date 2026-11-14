import { useCallback, useEffect, useState } from "react";
import { useApp } from "../app/AppContext";
import { RenewPanel } from "../components/Renew";
import { Alert, Page, Stat, formatWhen } from "../components/ui";
import type { OutboxStatus } from "../lib/db";
import { DAY_MS, listItems, type OutboxItem } from "../lib/outbox";
import { getKeys } from "../lib/session";

const EXPLAIN: Record<OutboxStatus, string> = {
  pending: "Waiting to be sent.",
  failed: "The server could not apply this record. It is kept and sent again at the next sync; if the error persists, tell the HIS Officer.",
  held: "Waiting for a conflict on a related record (usually the patient's registration) to be resolved at the facility.",
  conflict: "The facility record differs (possible duplicate, changed demographics or an open facility visit). The HIS Officer or Medical In-Charge decides; nothing is lost.",
  accepted: "Saved at the facility.",
  duplicate: "Already saved at the facility earlier.",
  resolved: "Conflict resolved at the facility.",
};

const STATUS_STYLE: Record<OutboxStatus, string> = {
  pending: "bg-slate-100 text-slate-800",
  failed: "bg-red-100 text-red-800",
  held: "bg-amber-100 text-amber-900",
  conflict: "bg-amber-100 text-amber-900",
  accepted: "bg-green-100 text-green-800",
  duplicate: "bg-green-100 text-green-800",
  resolved: "bg-green-100 text-green-800",
};

function ItemCard({ item, purgeDays }: { item: OutboxItem; purgeDays: number }) {
  const { row, secret, ack } = item;
  return (
    <div className="card space-y-1 text-sm">
      <div className="flex items-start justify-between gap-2">
        <span className="font-medium">{secret.summary}</span>
        <span className={`shrink-0 rounded px-2 py-0.5 text-xs font-semibold ${STATUS_STYLE[row.status]}`}>{row.status}</span>
      </div>
      <div className="text-slate-600">
        Captured {secret.envelope.captured_at}
        {row.attempts ? ` · sent ${row.attempts}×` : ""}
        {ack?.name ? ` · ${ack.doctype ?? ""} ${ack.name}` : ""}
        {ack?.conflict ? ` · conflict ${ack.conflict}` : ""}
        {ack?.discarded ? " · facility version kept" : ""}
      </div>
      {ack?.error && (row.status === "failed" || row.status === "pending") ? <div className="text-red-800">{ack.error}</div> : null}
      {row.status !== "accepted" && row.status !== "duplicate" ? <div className="text-slate-700">{EXPLAIN[row.status]}</div> : null}
      {row.final && row.acked_at ? (
        <div className="text-xs text-slate-500">Removed from this device after {formatWhen(row.acked_at + purgeDays * DAY_MS)}.</div>
      ) : null}
    </div>
  );
}

export default function SyncScreen() {
  const app = useApp();
  const { counts, syncState, syncing, online, authRequired, profile } = app;
  const [items, setItems] = useState<OutboxItem[]>([]);
  const [message, setMessage] = useState<string | null>(null);

  const load = useCallback(async () => {
    const keys = getKeys();
    if (keys) setItems(await listItems(keys));
  }, []);

  useEffect(() => {
    void load();
  }, [load, counts]);

  async function syncNow() {
    setMessage(null);
    const report = await app.sync("manual");
    if (!report) setMessage("Sync is not possible right now (the app is locked or offline).");
    else if (report.stoppedBy === "network") setMessage("No connection. Records are kept; sync continues when you are online.");
    else if (report.stoppedBy) setMessage(report.error);
    else setMessage(`Sent ${report.sent} record(s)${report.resolved ? `, ${report.resolved} conflict(s) resolved` : ""}.`);
    await load();
  }

  const attention = items.filter((i) => ["failed", "held", "conflict"].includes(i.row.status));
  const pending = items.filter((i) => i.row.status === "pending");
  const done = items.filter((i) => i.row.final);
  const purgeDays = profile?.settings.purge_after_days ?? 7;
  return (
    <Page title="Sync" back="/">
      {authRequired ? <RenewPanel reason="The server did not accept this device. Sign in online to keep syncing; nothing has been lost." onDone={() => void syncNow()} /> : null}
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <Stat label="Waiting to send" value={counts?.pending ?? 0} />
        <Stat label="Failed" value={counts?.failed ?? 0} />
        <Stat label="Held / conflict" value={(counts?.held ?? 0) + (counts?.conflict ?? 0)} />
        <Stat label="Synced (kept)" value={(counts?.accepted ?? 0) + (counts?.duplicate ?? 0) + (counts?.resolved ?? 0)} />
      </div>
      <div className="text-sm text-slate-600">
        Last sync attempt: {formatWhen(syncState?.last_run_at)} · last success: {formatWhen(syncState?.last_success_at)}
      </div>
      {syncState?.last_error ? <Alert kind="warn">{syncState.last_error}</Alert> : null}
      {message ? <Alert>{message}</Alert> : null}
      <button type="button" className="btn-primary w-full" disabled={syncing || !online} onClick={syncNow}>
        {syncing ? "Syncing…" : online ? "Sync now" : "Offline"}
      </button>

      {attention.length ? (
        <section className="space-y-2">
          <h2 className="font-semibold">Needs attention</h2>
          {attention.map((i) => (
            <ItemCard key={i.row.client_uuid} item={i} purgeDays={purgeDays} />
          ))}
        </section>
      ) : null}
      {pending.length ? (
        <section className="space-y-2">
          <h2 className="font-semibold">Waiting to send</h2>
          {pending.map((i) => (
            <ItemCard key={i.row.client_uuid} item={i} purgeDays={purgeDays} />
          ))}
        </section>
      ) : null}
      {done.length ? (
        <section className="space-y-2">
          <h2 className="font-semibold">Synced</h2>
          {done.map((i) => (
            <ItemCard key={i.row.client_uuid} item={i} purgeDays={purgeDays} />
          ))}
        </section>
      ) : null}
      {!items.length ? <p className="text-slate-600">Nothing recorded on this device yet.</p> : null}
    </Page>
  );
}
