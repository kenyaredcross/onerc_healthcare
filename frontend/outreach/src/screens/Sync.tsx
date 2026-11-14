import { useCallback, useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useApp } from "../app/AppContext";
import { RenewPanel } from "../components/Renew";
import { SessionEndedPanel } from "../components/SessionEnded";
import { Alert, Page, Stat, formatWhen } from "../components/ui";
import type { OutboxStatus } from "../lib/db";
import { DAY_MS, listItems, type HistoryEntry, type OutboxItem } from "../lib/outbox";
import { getKeys } from "../lib/session";

const EXPLAIN: Record<OutboxStatus, string> = {
  pending: "Waiting to be sent.",
  failed:
    "The server could not apply this record. It is kept and sent again at every sync. If the error is in what was entered, correct it and send it again; otherwise tell the HIS Officer.",
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

function historyLine(h: HistoryEntry): string {
  if (h.kind === "corrected") return `${formatWhen(h.at)}: corrected on this device and queued again`;
  const times = (h.count ?? 1) > 1 ? ` (${h.count}×, last ${formatWhen(h.last_at)})` : "";
  return `${formatWhen(h.at)}: refused${times}: ${h.error ?? "no reason given"}`;
}

function ItemCard({ item, purgeDays, statusOf }: { item: OutboxItem; purgeDays: number; statusOf: Map<string, OutboxStatus> }) {
  const navigate = useNavigate();
  const { syncing } = useApp();
  const { row, secret, ack } = item;
  const history = secret.history ?? [];
  // a record waits while something it depends on is not accepted yet (F2)
  const blockedBy = row.final ? [] : row.depends_on.filter((d) => ["failed", "pending"].includes(statusOf.get(d) ?? ""));
  const depFailed = row.depends_on.some((d) => statusOf.get(d) === "failed");
  return (
    <div className="card space-y-1 text-sm" data-testid={`record-${row.client_uuid}`}>
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
      {ack?.error && row.status === "failed" ? <div className="text-red-800">{ack.error}</div> : null}
      {row.status !== "accepted" && row.status !== "duplicate" ? <div className="text-slate-700">{EXPLAIN[row.status]}</div> : null}
      {blockedBy.length && depFailed ? (
        <div className="text-amber-900">It depends on a record the server refused. It is sent again once that record is corrected and accepted.</div>
      ) : null}
      {row.status === "failed" ? (
        <button
          type="button"
          className="btn-secondary mt-1 min-h-11 px-3"
          disabled={syncing}
          onClick={() => navigate(`/sync/fix/${encodeURIComponent(row.client_uuid)}`)}
        >
          Correct and send again
        </button>
      ) : null}
      {history.length ? (
        <details>
          <summary className="cursor-pointer text-xs text-slate-600">History on this device ({history.length})</summary>
          <ul className="mt-1 list-disc space-y-0.5 pl-5 text-xs text-slate-600">
            {history.map((h, i) => (
              <li key={i}>{historyLine(h)}</li>
            ))}
          </ul>
        </details>
      ) : null}
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
  const statusOf = new Map(items.map((i) => [i.row.client_uuid, i.row.status] as const));
  return (
    <Page title="Sync" back="/">
      <SessionEndedPanel />
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
            <ItemCard key={i.row.client_uuid} item={i} purgeDays={purgeDays} statusOf={statusOf} />
          ))}
        </section>
      ) : null}
      {pending.length ? (
        <section className="space-y-2">
          <h2 className="font-semibold">Waiting to send</h2>
          {pending.map((i) => (
            <ItemCard key={i.row.client_uuid} item={i} purgeDays={purgeDays} statusOf={statusOf} />
          ))}
        </section>
      ) : null}
      {done.length ? (
        <section className="space-y-2">
          <h2 className="font-semibold">Synced</h2>
          {done.map((i) => (
            <ItemCard key={i.row.client_uuid} item={i} purgeDays={purgeDays} statusOf={statusOf} />
          ))}
        </section>
      ) : null}
      {!items.length ? <p className="text-slate-600">Nothing recorded on this device yet.</p> : null}
    </Page>
  );
}
