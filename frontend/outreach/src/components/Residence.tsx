import { useMemo, useState } from "react";
import type { ReferenceResidence } from "../lib/protocol";
import { TextInput } from "./ui";

/** Drill-down picker over the residence tree from the package (groups open, places are chosen). */
export function ResidencePicker({
  residences,
  value,
  onChange,
}: {
  residences: ReferenceResidence[];
  value: string;
  onChange: (name: string) => void;
}) {
  const byName = useMemo(() => new Map(residences.map((r) => [r.name, r])), [residences]);
  const children = useMemo(() => {
    const m = new Map<string, ReferenceResidence[]>();
    for (const r of residences) {
      const k = r.parent && byName.has(r.parent) ? r.parent : "";
      if (!m.has(k)) m.set(k, []);
      m.get(k)!.push(r);
    }
    for (const list of m.values()) list.sort((a, b) => (a.location_name || a.name).localeCompare(b.location_name || b.name));
    return m;
  }, [residences, byName]);
  const pathOf = (name: string): ReferenceResidence[] => {
    const out: ReferenceResidence[] = [];
    let cur = byName.get(name);
    let guard = 0;
    while (cur && guard++ < 20) {
      out.unshift(cur);
      cur = cur.parent ? byName.get(cur.parent) : undefined;
    }
    return out;
  };
  const [parent, setParent] = useState<string>(() => (value ? (byName.get(value)?.parent ?? "") : ""));
  const [q, setQ] = useState("");

  const label = (r: ReferenceResidence) => r.location_name || r.name;
  const query = q.trim().toLowerCase();
  const list = query
    ? residences.filter((r) => !r.is_group && label(r).toLowerCase().includes(query)).slice(0, 30)
    : (children.get(parent) ?? []);

  return (
    <div className="space-y-2 rounded-lg border border-slate-300 bg-white p-2">
      <div className="text-sm">
        {value ? (
          <span>
            Chosen: <strong>{pathOf(value).map(label).join(" / ")}</strong>{" "}
            <button type="button" className="text-krcs underline" onClick={() => onChange("")}>
              clear
            </button>
          </span>
        ) : (
          <span className="text-slate-600">No residence chosen</span>
        )}
      </div>
      <TextInput placeholder="Search a village or block" value={q} onChange={(e) => setQ(e.target.value)} aria-label="Search residence" />
      {!query ? (
        <div className="flex flex-wrap items-center gap-1 text-sm">
          <button type="button" className="rounded px-2 py-1 underline" onClick={() => setParent("")}>
            All
          </button>
          {parent
            ? pathOf(parent).map((r) => (
                <button key={r.name} type="button" className="rounded px-2 py-1 underline" onClick={() => setParent(r.name)}>
                  › {label(r)}
                </button>
              ))
            : null}
        </div>
      ) : null}
      <ul className="max-h-72 divide-y overflow-auto">
        {list.map((r) => (
          <li key={r.name}>
            <button
              type="button"
              className={`flex min-h-12 w-full items-center justify-between px-2 text-left ${value === r.name ? "bg-krcs-light font-semibold" : ""}`}
              onClick={() => {
                if (r.is_group && !query) setParent(r.name);
                else onChange(r.name);
              }}
            >
              <span>
                {query ? pathOf(r.name).map(label).join(" / ") : label(r)}
                {r.level ? <span className="ml-2 text-xs text-slate-500">{r.level}</span> : null}
              </span>
              {r.is_group && !query ? <span aria-hidden>›</span> : null}
            </button>
          </li>
        ))}
        {list.length === 0 ? <li className="p-2 text-sm text-slate-600">Nothing here.</li> : null}
      </ul>
      {parent && byName.get(parent) && !query ? (
        <button type="button" className="btn-secondary w-full text-sm" onClick={() => onChange(parent)}>
          Choose “{label(byName.get(parent)!)}”
        </button>
      ) : null}
    </div>
  );
}
