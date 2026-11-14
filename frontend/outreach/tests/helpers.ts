import { IDBFactory } from "fake-indexeddb";
import { apiConfig, setCsrfToken } from "../src/lib/api";
import { generateRawDataKey, importKeys, type Keys } from "../src/lib/crypto";
import { closeDb } from "../src/lib/db";
import { resetMemory } from "../src/lib/session";
import { clearReferenceCache } from "../src/lib/reference";

export async function freshDevice(): Promise<void> {
  await closeDb();
  globalThis.indexedDB = new IDBFactory();
  resetMemory();
  clearReferenceCache();
  setCsrfToken(null);
}

export async function testKeys(): Promise<Keys> {
  return importKeys(generateRawDataKey());
}

export interface Call {
  method: string;
  path: string; // e.g. "onerc_healthcare.outreach.sync.push"
  query: URLSearchParams;
  body: unknown;
  headers: Record<string, string>;
  credentials: RequestCredentials | undefined;
}

export type Handler = (call: Call) => unknown | Promise<unknown>;

export class HttpError {
  constructor(
    public status: number,
    public body: unknown = {},
  ) {}
}

/** Route `/api/method/<path>` to handlers; a handler may throw "network" or return HttpError. */
export function mockServer(handlers: Record<string, Handler>): { calls: Call[] } {
  const calls: Call[] = [];
  apiConfig.fetch = async (input: string, init?: RequestInit) => {
    const url = new URL(input, "http://emr.test");
    const path = url.pathname.replace(/^\/api\/method\//, "");
    const headers = (init?.headers ?? {}) as Record<string, string>;
    const call: Call = {
      method: init?.method ?? "GET",
      path,
      query: url.searchParams,
      body: init?.body ? JSON.parse(String(init.body)) : null,
      headers,
      credentials: init?.credentials,
    };
    calls.push(call);
    const handler = handlers[path];
    if (!handler) return new Response(JSON.stringify({ exc_type: "DoesNotExistError" }), { status: 404 });
    let out: unknown;
    try {
      out = await handler(call);
    } catch (e) {
      if (e === "network") throw new TypeError("Failed to fetch");
      throw e;
    }
    if (out instanceof HttpError) return new Response(JSON.stringify(out.body), { status: out.status });
    if (path === "login") return new Response(JSON.stringify(out), { status: 200 });
    return new Response(JSON.stringify({ message: out }), { status: 200, headers: { "Content-Type": "application/json" } });
  };
  return { calls };
}
