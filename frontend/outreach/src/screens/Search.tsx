import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { Page, TextInput } from "../components/ui";
import { fullName, searchPatients, type SearchHit } from "../lib/patients";
import { requireKeys } from "../lib/session";
import { ageLabel, siteToday } from "../lib/time";

export default function SearchScreen() {
  const [q, setQ] = useState("");
  const [hits, setHits] = useState<SearchHit[] | null>(null);

  useEffect(() => {
    const query = q.trim();
    if (query.length < 2) {
      setHits(null);
      return;
    }
    let cancelled = false;
    const t = setTimeout(() => {
      void searchPatients(requireKeys(), query).then((h) => {
        if (!cancelled) setHits(h);
      });
    }, 200);
    return () => {
      cancelled = true;
      clearTimeout(t);
    };
  }, [q]);

  const today = siteToday();
  return (
    <Page title="Find patient" back="/">
      <TextInput
        type="search"
        autoFocus
        placeholder="Name, UNHCR ID or other identifier"
        value={q}
        onChange={(e) => setQ(e.target.value)}
        aria-label="Search patients"
      />
      <p className="text-xs text-slate-500">Searches the patients on this device. Similar-sounding names are found too.</p>
      {hits !== null && hits.length === 0 ? <p className="text-slate-600">No patient found on this device.</p> : null}
      <ul className="space-y-2">
        {(hits ?? []).map(({ patient: p }) => (
          <li key={p.key}>
            <Link to={`/patients/${encodeURIComponent(p.key)}`} className="card block active:bg-slate-50">
              <div className="flex items-center justify-between gap-2">
                <span className="font-semibold">{fullName(p)}</span>
                <span className="text-sm text-slate-600">
                  {p.sex} {ageLabel(p.dob, Boolean(p.dob_estimated), today)}
                </span>
              </div>
              <div className="text-sm text-slate-600">
                {p.name ?? "New (registered on this device)"}
                {p.identifiers.length ? ` · ${p.identifiers.map((i) => `${i.identifier_type} ${i.identifier_value}`).join(", ")}` : ""}
              </div>
            </Link>
          </li>
        ))}
      </ul>
      <Link to="/patients/new" className="btn-secondary w-full">
        Register a new patient
      </Link>
    </Page>
  );
}
