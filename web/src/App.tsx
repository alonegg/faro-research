import { useEffect, useRef, useState } from "react";
import {
  api,
  askStream,
  type PersistedMessage,
  type ResearchStreamEvent,
  type SessionMeta,
  type ToolCallSummary,
} from "./api";
import { Markdown } from "./markdown";

const SUGGESTIONS = [
  "贵州茅台 PE_TTM 和近 4 季度 ROE",
  "宁德时代过去 6 个月有无董监高减持",
  "比亚迪 2024 vs 2025 营收和归母净利",
  "中芯国际最新一年现金流量表",
];

interface LiveTool {
  tool_call_id: string;
  name: string;
  args: Record<string, unknown>;
  status: "running" | "done";
  latency_ms?: number;
  error?: string | null;
}

interface UITurn {
  id: number;            // local id
  query: string;
  status: "pending" | "done" | "error";
  liveTools: LiveTool[];
  finalAnswer?: string;
  finalToolCalls?: ToolCallSummary[];
  latencyTotalMs?: number;
  turns?: number;
  error?: string;
  elapsedMs: number;
}

interface ServerInfo {
  provider: string;
  n_tools: number;
  version: string;
}

export function App() {
  const [sessions, setSessions] = useState<SessionMeta[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [history, setHistory] = useState<PersistedMessage[]>([]);
  const [turns, setTurns] = useState<UITurn[]>([]);
  const [input, setInput] = useState("");
  const [running, setRunning] = useState(false);
  const [info, setInfo] = useState<ServerInfo | null>(null);
  const nextTurnId = useRef(1);
  const tickRef = useRef<number | null>(null);
  const threadRef = useRef<HTMLDivElement>(null);

  // ── boot ──────────────────────────────────────────────────────────
  useEffect(() => {
    api.health().then((h) => setInfo({ provider: h.provider, n_tools: h.n_tools, version: h.version }))
      .catch(() => {});
    api.listSessions().then(setSessions).catch(() => {});
  }, []);

  // ── load active session messages ──────────────────────────────────
  useEffect(() => {
    if (!activeId) {
      setHistory([]);
      setTurns([]);
      return;
    }
    api.getSession(activeId).then((d) => {
      setHistory(d.messages);
      setTurns([]);  // live turns stay empty for a freshly opened session
    }).catch(() => {});
  }, [activeId]);

  // ── elapsed tick ──────────────────────────────────────────────────
  useEffect(() => {
    const pending = turns.find((t) => t.status === "pending");
    if (!pending) {
      if (tickRef.current) { clearInterval(tickRef.current); tickRef.current = null; }
      return;
    }
    if (tickRef.current) return;
    const start = Date.now() - pending.elapsedMs;
    tickRef.current = window.setInterval(() => {
      setTurns((prev) =>
        prev.map((t) =>
          t.id === pending.id && t.status === "pending"
            ? { ...t, elapsedMs: Date.now() - start } : t,
        ),
      );
    }, 500);
    return () => {
      if (tickRef.current) { clearInterval(tickRef.current); tickRef.current = null; }
    };
  }, [turns]);

  // ── auto-scroll ───────────────────────────────────────────────────
  useEffect(() => {
    threadRef.current?.scrollTo({ top: threadRef.current.scrollHeight, behavior: "smooth" });
  }, [turns, history]);

  // ── actions ───────────────────────────────────────────────────────
  const newSession = async () => {
    const s = await api.createSession();
    setSessions((prev) => [s, ...prev]);
    setActiveId(s.id);
  };

  const ensureSession = async (): Promise<string> => {
    if (activeId) return activeId;
    const s = await api.createSession();
    setSessions((prev) => [s, ...prev]);
    setActiveId(s.id);
    return s.id;
  };

  const onEvent = (turnId: number, ev: ResearchStreamEvent) => {
    setTurns((prev) =>
      prev.map((t) => {
        if (t.id !== turnId) return t;
        if (ev.type === "tool_call") {
          return {
            ...t,
            liveTools: [
              ...t.liveTools,
              { tool_call_id: ev.tool_call_id, name: ev.name, args: ev.args, status: "running" },
            ],
          };
        }
        if (ev.type === "tool_result") {
          return {
            ...t,
            liveTools: t.liveTools.map((lt) =>
              lt.tool_call_id === ev.tool_call_id
                ? { ...lt, latency_ms: ev.latency_ms, error: ev.error, status: "done" } : lt,
            ),
          };
        }
        if (ev.type === "final") {
          return {
            ...t,
            status: "done",
            finalAnswer: ev.answer,
            finalToolCalls: ev.tool_calls,
            latencyTotalMs: ev.latency_total_ms,
            turns: ev.turns,
            elapsedMs: ev.latency_total_ms,
          };
        }
        if (ev.type === "error") {
          return { ...t, status: "error", error: ev.message };
        }
        return t;
      }),
    );
  };

  const submit = async (raw: string) => {
    const query = raw.trim();
    if (!query || running) return;
    setRunning(true);
    setInput("");
    const sid = await ensureSession();
    const id = nextTurnId.current++;
    setTurns((prev) => [...prev, {
      id, query, status: "pending", liveTools: [], elapsedMs: 0,
    }]);
    try {
      await askStream(sid, query, (ev) => onEvent(id, ev));
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      setTurns((prev) => prev.map((t) =>
        t.id === id && t.status === "pending" ? { ...t, status: "error", error: msg } : t,
      ));
    } finally {
      setRunning(false);
      // refresh session list to bump updated_at order
      api.listSessions().then(setSessions).catch(() => {});
    }
  };

  const onKey = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
      e.preventDefault();
      submit(input);
    }
  };

  const deleteSession = async (id: string) => {
    if (!confirm("删除这个会话?")) return;
    await api.deleteSession(id);
    setSessions((prev) => prev.filter((s) => s.id !== id));
    if (activeId === id) setActiveId(null);
  };

  // ── render ────────────────────────────────────────────────────────
  return (
    <div className="app">
      <aside className="sidebar">
        <div className="sidebar__head">
          <span className="sidebar__brand-mark">F</span>
          <span className="sidebar__brand">Faro Research</span>
        </div>
        <div className="sidebar__new">
          <button className="btn btn--primary" style={{ width: "100%" }} onClick={newSession}>
            + 新会话
          </button>
        </div>
        <div className="session-list">
          {sessions.length === 0 && (
            <div style={{ padding: 16, color: "var(--ink-3)", fontSize: 12, textAlign: "center" }}>
              还没有会话<br />点上面新建
            </div>
          )}
          {sessions.map((s) => (
            <div
              key={s.id}
              className={"session-item " + (activeId === s.id ? "active" : "")}
              onClick={() => setActiveId(s.id)}
              style={{ cursor: "pointer" }}
            >
              <span className="session-item__title">{s.title}</span>
              <span className="session-item__date">
                {new Date(s.updated_at).toLocaleDateString("zh-CN", { month: "numeric", day: "numeric" })}
              </span>
              <button
                title="删除"
                style={{ color: "var(--ink-3)", fontSize: 14, padding: "0 4px" }}
                onClick={(e) => { e.stopPropagation(); deleteSession(s.id); }}
              >
                ×
              </button>
            </div>
          ))}
        </div>
      </aside>

      <div className="main">
        <header className="topbar">
          <span className="topbar__title">A 股研究助手</span>
          <span className="topbar__sub">DeepSeek + Tushare · 工具可插拔</span>
          {info && (
            <span className="topbar__pill">
              <span className="dot" />
              {info.provider} · {info.n_tools} tools · v{info.version}
            </span>
          )}
        </header>

        <div className="thread" ref={threadRef}>
          {!activeId && turns.length === 0 && (
            <div className="thread__empty">
              <h2>问个 A 股研究问题</h2>
              <div>例如:</div>
              <div className="suggestions">
                {SUGGESTIONS.map((s) => (
                  <button key={s} className="btn btn--ghost" style={{ maxWidth: 520 }}
                          onClick={() => submit(s)}>
                    {s}
                  </button>
                ))}
              </div>
            </div>
          )}

          {/* Persisted history (loaded from server) */}
          {history.map((m) => (
            <PersistedMessageView key={m.seq} m={m} />
          ))}

          {/* Live turns */}
          {turns.map((t) => <TurnView key={t.id} turn={t} />)}
        </div>

        <div className="composer">
          <div className="composer__row">
            <textarea
              placeholder={running ? "等待中..." : "问个 A 股研究问题... (⌘/Ctrl + Enter 发送)"}
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={onKey}
              rows={2}
              disabled={running}
            />
            <button
              className="btn btn--primary"
              onClick={() => submit(input)}
              disabled={running || !input.trim()}
            >
              {running ? "⋯" : "提问"}
            </button>
          </div>
          <div className="composer__hint">
            数据：本地 SQLite 多会话历史 + Tushare（行情 / 三表 / 估值 / 高管交易）·
            自动写审计日志
          </div>
        </div>
      </div>
    </div>
  );
}

// ──────────────────────────────────────────────────────────────────────────
// Render helpers
// ──────────────────────────────────────────────────────────────────────────

function PersistedMessageView({ m }: { m: PersistedMessage }) {
  if (m.role === "user") {
    return <div className="user-bubble">{m.content}</div>;
  }
  if (m.role === "assistant") {
    const meta = m.meta as { turns?: number; tool_calls?: ToolCallSummary[]; latency_total_ms?: number };
    return (
      <>
        {meta.tool_calls && meta.tool_calls.length > 0 && (
          <ToolsTrace
            tools={meta.tool_calls.map((tc, i) => ({
              tool_call_id: String(i),
              name: tc.name,
              args: tc.args,
              status: "done" as const,
              latency_ms: tc.latency_ms,
              error: tc.error,
            }))}
            expanded={false}
          />
        )}
        <div className="assistant-card"><Markdown text={m.content} /></div>
        {meta.turns !== undefined && (
          <div className="run-meta">
            <span>{meta.turns} turns</span>
            <span>{(meta.tool_calls?.length ?? 0)} tool calls</span>
            <span>{((meta.latency_total_ms ?? 0) / 1000).toFixed(1)} s</span>
          </div>
        )}
      </>
    );
  }
  return null;  // skip system / tool rows
}

function TurnView({ turn }: { turn: UITurn }) {
  const isPending = turn.status === "pending";
  return (
    <>
      <div className="user-bubble">{turn.query}</div>
      {turn.liveTools.length > 0 && (
        <ToolsTrace tools={turn.liveTools} expanded={isPending} />
      )}
      {isPending && (
        <div className="pending">
          <span className="dot" /> 思考中… {(turn.elapsedMs / 1000).toFixed(1)} s
        </div>
      )}
      {turn.status === "error" && (
        <div className="error-card">⚠ {turn.error}</div>
      )}
      {turn.status === "done" && turn.finalAnswer && (
        <>
          <div className="assistant-card"><Markdown text={turn.finalAnswer} /></div>
          <div className="run-meta">
            <span>{turn.turns} turns</span>
            <span>{turn.finalToolCalls?.length ?? 0} tool calls</span>
            <span>{((turn.latencyTotalMs ?? 0) / 1000).toFixed(1)} s</span>
          </div>
        </>
      )}
    </>
  );
}

function ToolsTrace({ tools, expanded }: { tools: LiveTool[]; expanded: boolean }) {
  return (
    <details className="tools-trace" open={expanded}>
      <summary>
        工具调用 ({tools.length}
        {tools.some((t) => t.status === "running") && (
          <span style={{ color: "var(--accent)" }}> · 进行中</span>
        )}
        )
      </summary>
      {tools.map((t) => {
        const argStr = Object.entries(t.args).map(([k, v]) => `${k}=${JSON.stringify(v)}`).join(", ");
        const pillKind = t.error ? "neg" : t.status === "running" ? "warn" : "info";
        return (
          <div key={t.tool_call_id} className="tool-line">
            <span className={`tool-pill tool-pill--${pillKind}`}>{t.name}</span>{" "}
            <span>({argStr})</span>{" "}
            <span>
              {t.status === "running"
                ? "— 运行中…"
                : `— ${(t.latency_ms ?? 0).toFixed(0)} ms${t.error ? `, 错误: ${t.error}` : ""}`}
            </span>
          </div>
        );
      })}
    </details>
  );
}
