/** API client for Faro Research server. */

export interface SessionMeta {
  id: string;
  title: string;
  created_at: string;
  updated_at: string;
}

export interface PersistedMessage {
  seq: number;
  role: "system" | "user" | "assistant" | "tool";
  content: string;
  meta: Record<string, unknown>;
  created_at: string;
}

export interface ToolCallSummary {
  name: string;
  args: Record<string, unknown>;
  latency_ms: number;
  result_chars: number;
  error: string | null;
}

export interface AskResponse {
  final_answer: string;
  turns: number;
  tool_calls: ToolCallSummary[];
  latency_total_ms: number;
  error: string | null;
}

export interface ToolInfo {
  name: string;
  description: string;
  parameters: Record<string, unknown>;
}

// API key persisted in localStorage; sent as Bearer on every request.
// In auth-disabled deployments (FARO_AUTH_REQUIRED unset on backend) the
// header is ignored; in auth-required deployments, missing/invalid → 401.
const API_KEY_STORAGE = "faro_api_key";

export function getApiKey(): string {
  return localStorage.getItem(API_KEY_STORAGE) || "";
}

export function setApiKey(key: string): void {
  if (key) localStorage.setItem(API_KEY_STORAGE, key);
  else localStorage.removeItem(API_KEY_STORAGE);
}

function authHeaders(extra?: Record<string, string>): Record<string, string> {
  const h: Record<string, string> = { ...(extra || {}) };
  const k = getApiKey();
  if (k) h["authorization"] = `Bearer ${k}`;
  return h;
}

async function jget<T>(path: string): Promise<T> {
  const r = await fetch(`/api${path}`, { headers: authHeaders() });
  if (!r.ok) throw new Error(`${r.status}: ${await r.text()}`);
  return r.json() as Promise<T>;
}

async function jpost<T>(path: string, body: unknown): Promise<T> {
  const r = await fetch(`/api${path}`, {
    method: "POST",
    headers: authHeaders({ "content-type": "application/json" }),
    body: JSON.stringify(body),
  });
  if (!r.ok) throw new Error(`${r.status}: ${await r.text()}`);
  return r.json() as Promise<T>;
}

export interface MeResponse {
  id: string;
  email: string;
  role: string;
  created_at: string;
}

export interface HealthResponse {
  status: string;
  version: string;
  provider: string;
  auth_required: boolean;
}

export const api = {
  health: () => jget<HealthResponse>("/health"),
  me: () => jget<MeResponse>("/auth/me"),
  tools: () => jget<ToolInfo[]>("/tools"),
  listSessions: () => jget<SessionMeta[]>("/sessions"),
  createSession: (title?: string) => jpost<SessionMeta>("/sessions", { title }),
  getSession: (id: string) =>
    jget<{ session: SessionMeta; messages: PersistedMessage[] }>(`/sessions/${id}`),
  rename: (id: string, title: string) =>
    fetch(`/api/sessions/${id}`, {
      method: "PATCH",
      headers: authHeaders({ "content-type": "application/json" }),
      body: JSON.stringify({ title }),
    }).then(async (r) => {
      if (!r.ok) throw new Error(`${r.status}: ${await r.text()}`);
      return r.json() as Promise<SessionMeta>;
    }),
  deleteSession: (id: string) =>
    fetch(`/api/sessions/${id}`, { method: "DELETE", headers: authHeaders() })
      .then(async (r) => {
        if (!r.ok) throw new Error(`${r.status}: ${await r.text()}`);
        return r.json();
      }),
  /** Triggers a browser download. Returns the URL we'd hit (useful for a tags). */
  exportUrl: (id: string, fmt: "md" | "pdf") => `/api/sessions/${id}/export.${fmt}`,
  download: async (id: string, fmt: "md" | "pdf") => {
    const r = await fetch(`/api/sessions/${id}/export.${fmt}`, { headers: authHeaders() });
    if (!r.ok) throw new Error(`${r.status}: ${await r.text()}`);
    const blob = await r.blob();
    const cd = r.headers.get("content-disposition") || "";
    const m = cd.match(/filename\*=UTF-8''([^;]+)/i) || cd.match(/filename="([^"]+)"/);
    const filename = m ? decodeURIComponent(m[1]) : `${id}.${fmt}`;
    const link = document.createElement("a");
    link.href = URL.createObjectURL(blob);
    link.download = filename;
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
    setTimeout(() => URL.revokeObjectURL(link.href), 1000);
  },
};

export type ResearchStreamEvent =
  | { type: "turn_start"; turn: number }
  | { type: "tool_call"; tool_call_id: string; name: string; args: Record<string, unknown> }
  | {
      type: "tool_result";
      tool_call_id: string;
      name: string;
      latency_ms: number;
      result_chars: number;
      error: string | null;
    }
  | {
      type: "final";
      answer: string;
      turns: number;
      tool_calls: ToolCallSummary[];
      latency_total_ms: number;
    }
  | { type: "error"; message: string }
  | { type: "done" };

/** Stream the agent's response. EventSource doesn't support POST, so we
 *  manually parse SSE off a fetch ReadableStream. */
// eslint-disable-next-line @typescript-eslint/no-explicit-any
export async function askStream(
  sessionId: string,
  query: string,
  onEvent: (e: ResearchStreamEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  const r = await fetch(`/api/sessions/${sessionId}/ask/stream`, {
    method: "POST",
    headers: authHeaders({
      "content-type": "application/json",
      accept: "text/event-stream",
    }),
    body: JSON.stringify({ query }),
    signal,
  });
  if (!r.ok) throw new Error(`${r.status}: ${await r.text()}`);
  if (!r.body) throw new Error("no response body");
  const reader = r.body.getReader();
  const decoder = new TextDecoder("utf-8");
  let buf = "";
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    let idx;
    while ((idx = buf.indexOf("\n\n")) >= 0) {
      const chunk = buf.slice(0, idx);
      buf = buf.slice(idx + 2);
      const dataLine = chunk.split("\n").find((l) => l.startsWith("data:"));
      if (!dataLine) continue;
      const json = dataLine.slice(5).trim();
      if (!json) continue;
      try {
        const ev = JSON.parse(json) as ResearchStreamEvent;
        onEvent(ev);
        if (ev.type === "done") return;
      } catch {
        // skip malformed
      }
    }
  }
}
