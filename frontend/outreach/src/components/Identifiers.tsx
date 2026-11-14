import type { Identifier, ReferenceIdentifierType } from "../lib/protocol";
import { Select, TextInput } from "./ui";

export function IdentifiersEditor({
  types,
  value,
  onChange,
  errors,
}: {
  types: ReferenceIdentifierType[];
  value: Identifier[];
  onChange: (v: Identifier[]) => void;
  errors: Record<string, string>;
}) {
  const usable = types.filter((t) => !t.facility_generated);
  const set = (idx: number, patch: Partial<Identifier>) => onChange(value.map((v, i) => (i === idx ? { ...v, ...patch } : v)));
  return (
    <div className="space-y-2">
      {value.map((id, idx) => (
        <div key={idx}>
          <div className="flex gap-2">
            <Select
              className="w-2/5"
              aria-label="Identifier type"
              options={usable.map((t) => ({ value: t.name, label: `${t.name}${t.is_primary ? " *" : ""}` }))}
              placeholder="Type…"
              value={id.identifier_type}
              onChange={(e) => set(idx, { identifier_type: e.target.value })}
            />
            <TextInput
              className="flex-1"
              aria-label="Identifier value"
              autoCapitalize="characters"
              value={id.identifier_value}
              onChange={(e) => set(idx, { identifier_value: e.target.value })}
            />
            <button type="button" className="btn-secondary px-3" aria-label="Remove identifier" onClick={() => onChange(value.filter((_, i) => i !== idx))}>
              ×
            </button>
          </div>
          {errors[`identifier_${idx}`] ? <p className="field-error">{errors[`identifier_${idx}`]}</p> : null}
        </div>
      ))}
      <button type="button" className="btn-secondary text-sm" onClick={() => onChange([...value, { identifier_type: usable[0]?.name ?? "", identifier_value: "" }])}>
        + Add identifier
      </button>
      <p className="text-xs text-slate-500">* primary identifier</p>
    </div>
  );
}
