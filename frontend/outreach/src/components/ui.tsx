import type { InputHTMLAttributes, ReactNode, SelectHTMLAttributes, TextareaHTMLAttributes } from "react";
import { Link, useNavigate } from "react-router-dom";
import { useApp } from "../app/AppContext";

export function OnlineBadge() {
  const { online, syncing } = useApp();
  return (
    <span
      className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs font-semibold ${
        online ? "bg-green-100 text-green-800" : "bg-amber-100 text-amber-900"
      }`}
      aria-live="polite"
    >
      <span className={`h-2 w-2 rounded-full ${online ? "bg-green-600" : "bg-amber-600"}`} />
      {online ? (syncing ? "Syncing…" : "Online") : "Offline"}
    </span>
  );
}

export function Page({
  title,
  back,
  actions,
  children,
}: {
  title: string;
  back?: string | true;
  actions?: ReactNode;
  children: ReactNode;
}) {
  const navigate = useNavigate();
  return (
    <div className="mx-auto flex min-h-screen max-w-3xl flex-col">
      <header className="sticky top-0 z-10 flex items-center gap-2 border-b border-slate-200 bg-white/95 px-3 py-2 backdrop-blur">
        {back ? (
          <button
            type="button"
            className="btn-secondary min-h-11 px-3"
            onClick={() => (back === true ? navigate(-1) : navigate(back))}
            aria-label="Back"
          >
            ←
          </button>
        ) : (
          <Link to="/" className="flex h-11 w-11 items-center justify-center" aria-label="Home">
            <img src={`${import.meta.env.BASE_URL}icons/icon.svg`} alt="" className="h-8 w-8" />
          </Link>
        )}
        <h1 className="min-w-0 flex-1 truncate text-lg font-bold">{title}</h1>
        {actions}
        <OnlineBadge />
      </header>
      <main className="flex-1 space-y-4 p-3 pb-24 sm:p-4">{children}</main>
    </div>
  );
}

export function Alert({ kind = "info", children }: { kind?: "info" | "error" | "warn" | "ok"; children: ReactNode }) {
  const cls = {
    info: "border-sky-200 bg-sky-50 text-sky-900",
    error: "border-red-200 bg-red-50 text-red-900",
    warn: "border-amber-200 bg-amber-50 text-amber-900",
    ok: "border-green-200 bg-green-50 text-green-900",
  }[kind];
  return (
    <div role={kind === "error" ? "alert" : "status"} className={`rounded-lg border p-3 text-sm ${cls}`}>
      {children}
    </div>
  );
}

export function Field({
  label,
  error,
  hint,
  children,
  htmlFor,
}: {
  label: string;
  error?: string | undefined;
  hint?: string;
  children: ReactNode;
  htmlFor?: string;
}) {
  return (
    <div>
      <label className="label" htmlFor={htmlFor}>
        {label}
      </label>
      {children}
      {hint && !error ? <p className="mt-1 text-xs text-slate-500">{hint}</p> : null}
      {error ? <p className="field-error">{error}</p> : null}
    </div>
  );
}

export function TextInput(props: InputHTMLAttributes<HTMLInputElement>) {
  return <input {...props} className={`input ${props.className ?? ""}`} />;
}

export function TextArea(props: TextareaHTMLAttributes<HTMLTextAreaElement>) {
  return <textarea rows={3} {...props} className={`input ${props.className ?? ""}`} />;
}

export function Select({
  options,
  placeholder,
  ...props
}: SelectHTMLAttributes<HTMLSelectElement> & { options: (string | { value: string; label: string })[]; placeholder?: string }) {
  return (
    <select {...props} className={`input ${props.className ?? ""}`}>
      {placeholder !== undefined ? <option value="">{placeholder}</option> : null}
      {options.map((o) =>
        typeof o === "string" ? (
          <option key={o} value={o}>
            {o}
          </option>
        ) : (
          <option key={o.value} value={o.value}>
            {o.label}
          </option>
        ),
      )}
    </select>
  );
}

export function Choice({
  options,
  value,
  onChange,
  name,
}: {
  options: string[];
  value: string;
  onChange: (v: string) => void;
  name: string;
}) {
  return (
    <div className="flex flex-wrap gap-2" role="radiogroup" aria-label={name}>
      {options.map((o) => (
        <button
          key={o}
          type="button"
          role="radio"
          aria-checked={value === o}
          onClick={() => onChange(o)}
          className={`btn ${value === o ? "bg-krcs text-white" : "border border-slate-300 bg-white text-slate-800"}`}
        >
          {o}
        </button>
      ))}
    </div>
  );
}

export function Stat({ label, value, to }: { label: string; value: ReactNode; to?: string }) {
  const body = (
    <div className="card flex flex-col">
      <span className="text-2xl font-bold">{value}</span>
      <span className="text-sm text-slate-600">{label}</span>
    </div>
  );
  return to ? <Link to={to}>{body}</Link> : body;
}

export function Spinner({ label = "Loading…" }: { label?: string }) {
  return (
    <div className="flex items-center justify-center gap-3 p-8 text-slate-600" role="status">
      <span className="h-6 w-6 animate-spin rounded-full border-4 border-slate-300 border-t-krcs" />
      {label}
    </div>
  );
}

export function errorText(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

export function formatWhen(epochMs: number | null | undefined): string {
  if (!epochMs) return "never";
  const d = new Date(epochMs);
  return d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}
