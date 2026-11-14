/**
 * HTTP client for the Frappe endpoints (protocol §1).
 *
 * - "cookie" calls run after an online sign-in (`/api/method/login`): the browser session cookie plus
 *   the CSRF token from the /outreach boot on POST.
 * - "device" calls carry `Authorization: krcs-outreach <grant>:<secret>` and no cookie; the server only
 *   accepts them on sync.push, sync.status, package.get_reference and package.get_patients.
 */
import { AUTH_SCHEME, METHOD_PREFIX } from "./protocol";

export type FetchLike = (input: string, init?: RequestInit) => Promise<Response>;

/** The request never reached the server or no response came back: retry later, keep everything. */
export class NetworkError extends Error {
  constructor(message = "No connection to the server") {
    super(message);
    this.name = "NetworkError";
  }
}

/** The server answered with an error status. 401 means an online sign-in is needed. */
export class ApiError extends Error {
  status: number;
  excType: string | null;
  constructor(status: number, message: string, excType: string | null = null) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.excType = excType;
  }
  get needsSignIn(): boolean {
    return this.status === 401;
  }
  /** Worth retrying later without user action (server busy or temporarily down). */
  get transient(): boolean {
    return this.status === 429 || this.status === 408 || this.status >= 500;
  }
}

export interface DeviceCredentials {
  grant: string;
  secret: string;
}

export interface ApiConfig {
  baseUrl: string;
  csrfToken: string | null;
  fetch: FetchLike;
}

export const apiConfig: ApiConfig = {
  baseUrl: "",
  csrfToken: null,
  fetch: (input, init) => globalThis.fetch(input, init),
};

export function setCsrfToken(token: string | null | undefined): void {
  apiConfig.csrfToken = token && token !== "None" ? token : null;
}

function stripHtml(s: string): string {
  return s.replace(/<[^>]*>/g, "").replace(/&nbsp;/g, " ").trim();
}

/** Readable message from Frappe's error JSON (`_server_messages`, `exception`, `message`). */
export function frappeErrorMessage(body: unknown, fallback: string): string {
  if (body && typeof body === "object") {
    const b = body as Record<string, unknown>;
    if (typeof b._server_messages === "string") {
      try {
        const msgs = (JSON.parse(b._server_messages) as string[]).map((m) => {
          try {
            const parsed = JSON.parse(m) as { message?: string };
            return stripHtml(parsed.message ?? m);
          } catch {
            return stripHtml(m);
          }
        });
        if (msgs.length) return msgs.join("\n");
      } catch {
        /* fall through */
      }
    }
    if (typeof b.exception === "string") return stripHtml(b.exception.split(":").slice(1).join(":") || b.exception);
    if (typeof b.message === "string") return stripHtml(b.message);
  }
  return fallback;
}

export interface CallOptions {
  method?: "GET" | "POST";
  params?: Record<string, unknown>;
  body?: unknown;
  device?: DeviceCredentials;
  signal?: AbortSignal;
}

function query(params: Record<string, unknown> | undefined): string {
  if (!params) return "";
  const q = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v === undefined || v === null) continue;
    q.set(k, typeof v === "string" ? v : JSON.stringify(v));
  }
  const s = q.toString();
  return s ? `?${s}` : "";
}

/** Call `/api/method/<path>` and return Frappe's `message`. */
export async function callMethod<T>(path: string, opts: CallOptions = {}): Promise<T> {
  const method = opts.method ?? "GET";
  const headers: Record<string, string> = { Accept: "application/json" };
  const init: RequestInit = { method, headers, signal: opts.signal ?? null };
  if (opts.device) {
    headers.Authorization = `${AUTH_SCHEME} ${opts.device.grant}:${opts.device.secret}`;
    init.credentials = "omit";
  } else {
    init.credentials = "same-origin";
    if (method === "POST" && apiConfig.csrfToken) headers["X-Frappe-CSRF-Token"] = apiConfig.csrfToken;
  }
  if (method === "POST") {
    headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(opts.body ?? {});
  }
  const url = `${apiConfig.baseUrl}/api/method/${path}${method === "GET" ? query(opts.params) : ""}`;
  let res: Response;
  try {
    res = await apiConfig.fetch(url, init);
  } catch (e) {
    if (e instanceof DOMException && e.name === "AbortError") throw e;
    throw new NetworkError();
  }
  let body: unknown = null;
  try {
    body = await res.json();
  } catch {
    if (res.ok) throw new NetworkError("The server sent an unreadable answer");
  }
  if (!res.ok) {
    const excType = body && typeof body === "object" ? ((body as { exc_type?: string }).exc_type ?? null) : null;
    throw new ApiError(res.status, frappeErrorMessage(body, `${res.status} ${res.statusText}`), excType);
  }
  return (body as { message: T }).message;
}

export const outreachMethod = (fn: string): string => `${METHOD_PREFIX.replace("/api/method/", "")}${fn}`;

// ----- endpoints ------------------------------------------------------------------------------

export interface BootInfo {
  site_name: string;
  csrf_token: string | null;
  timezone: string;
  user: { name: string; full_name: string } | null;
  sw_url: string;
  build: string | null;
}

/** Boot info (CSRF token, user) after a sign-in, or in `yarn dev` where the page has no server boot. */
export function fetchBoot(): Promise<BootInfo> {
  return callMethod<BootInfo>("onerc_healthcare.www.outreach.get_boot");
}

export async function login(usr: string, pwd: string): Promise<{ full_name?: string }> {
  // /api/method/login answers at the top level, not inside `message`
  let res: Response;
  try {
    res = await apiConfig.fetch(`${apiConfig.baseUrl}/api/method/login`, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify({ usr, pwd }),
    });
  } catch {
    throw new NetworkError();
  }
  let body: unknown = null;
  try {
    body = await res.json();
  } catch {
    /* ignore */
  }
  if (!res.ok) throw new ApiError(res.status, frappeErrorMessage(body, "Sign-in failed"));
  return (body ?? {}) as { full_name?: string };
}
