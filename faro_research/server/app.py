"""FastAPI server — sessions, messages, streaming agent calls.

Endpoints:
    GET  /api/health
    GET  /api/tools                           — list registered tools
    GET  /api/sessions                        — list sessions
    POST /api/sessions                        — create
    GET  /api/sessions/{id}                   — fetch one + its messages
    PATCH /api/sessions/{id}                  — rename
    DELETE /api/sessions/{id}
    POST /api/sessions/{id}/ask               — batch
    POST /api/sessions/{id}/ask/stream        — SSE
    GET  /api/audit                           — recent events

All routes share one `Agent`, one `SessionStore`, one `ToolRegistry`.
Customise the registry by setting `_tool_registry` BEFORE the first request,
or by importing `make_app(registry=..., agent=...)` from your own bootstrap.
"""

from __future__ import annotations

import json

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from faro_research.agent import Agent, Message, build_system_prompt
from faro_research.audit import SessionStore
from faro_research.config import settings
from faro_research.memory import MemoryStore, make_memory_tools
from faro_research.providers import Provider, make_provider
from faro_research.skills import make_skill_tool
from faro_research.tools import ToolRegistry
from faro_research.tools.builtin.tushare import tushare_default_tools

# ────────────────────────────────────────────────────────────────────────────
# Default singletons — override via `make_app()`
# ────────────────────────────────────────────────────────────────────────────


def _default_registry(provider: Provider, memory: MemoryStore) -> ToolRegistry:
    """Default agent tools.

    Layers (in order added):
      1. Tushare meta-tool + get_stock_quote (data)
      2. skill tool (workflows, if any SKILL.md files discovered)
      3. memory_search / memory_get / memory_update (recall)
    """
    reg = ToolRegistry()
    reg.register_many(tushare_default_tools(provider))
    skill = make_skill_tool()
    if skill is not None:
        reg.register(skill)
    reg.register_many(make_memory_tools(memory))
    return reg


# ────────────────────────────────────────────────────────────────────────────
# Pydantic IO
# ────────────────────────────────────────────────────────────────────────────


class CreateSessionBody(BaseModel):
    title: str | None = None


class RenameBody(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)


class AskBody(BaseModel):
    query: str = Field(..., min_length=1, max_length=4000)


class AskResponse(BaseModel):
    final_answer: str
    turns: int
    tool_calls: list[dict]
    latency_total_ms: float
    error: str | None = None


class ToolInfo(BaseModel):
    name: str
    description: str
    parameters: dict


class SessionOut(BaseModel):
    id: str
    title: str
    created_at: str
    updated_at: str


class MessageOut(BaseModel):
    seq: int
    role: str
    content: str
    meta: dict
    created_at: str


# ────────────────────────────────────────────────────────────────────────────
# App factory
# ────────────────────────────────────────────────────────────────────────────


def make_app(
    *,
    registry: ToolRegistry | None = None,
    agent: Agent | None = None,
    store: SessionStore | None = None,
) -> FastAPI:
    """Build a configured FastAPI app. Call this from a custom bootstrap to
    inject your own tool registry / agent / store.

    For zero-config use, `from faro_research.server.app import app` works
    (it calls `make_app()` with defaults at import time)."""
    provider = make_provider()
    memory = MemoryStore()
    reg = registry or _default_registry(provider, memory)
    sys_prompt = build_system_prompt(soul=memory.soul(), rules=memory.rules())
    ag = agent or Agent(provider=provider, tools=reg, system_prompt=sys_prompt)
    st = store or SessionStore()

    app = FastAPI(
        title="Faro Research",
        version="0.1.0",
        description="A-share research agent — pluggable tools + SSE streaming.",
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ── meta ────────────────────────────────────────────────────────────

    @app.get("/api/health")
    def health() -> dict:
        return {
            "status": "ok",
            "version": "0.1.0",
            "provider": ag.provider.name,
            "n_tools": len(reg),
        }

    @app.get("/api/tools", response_model=list[ToolInfo])
    def list_tools() -> list[ToolInfo]:
        return [
            ToolInfo(name=s.name, description=s.description, parameters=s.parameters)
            for s in reg.specs()
        ]

    # ── sessions ────────────────────────────────────────────────────────

    @app.get("/api/sessions", response_model=list[SessionOut])
    def list_sessions() -> list[SessionOut]:
        return [
            SessionOut(
                id=s.id, title=s.title,
                created_at=s.created_at.isoformat(),
                updated_at=s.updated_at.isoformat(),
            )
            for s in st.list_sessions()
        ]

    @app.post("/api/sessions", response_model=SessionOut)
    def create_session(body: CreateSessionBody) -> SessionOut:
        s = st.create_session(title=body.title)
        return SessionOut(
            id=s.id, title=s.title,
            created_at=s.created_at.isoformat(),
            updated_at=s.updated_at.isoformat(),
        )

    @app.get("/api/sessions/{session_id}")
    def get_session(session_id: str) -> dict:
        s = st.get_session(session_id)
        if not s:
            raise HTTPException(404, f"session {session_id} not found")
        msgs = [
            MessageOut(
                seq=m.seq, role=m.role, content=m.content,
                meta=json.loads(m.meta_json) if m.meta_json else {},
                created_at=m.created_at.isoformat(),
            )
            for m in st.list_messages(session_id)
        ]
        return {
            "session": SessionOut(
                id=s.id, title=s.title,
                created_at=s.created_at.isoformat(),
                updated_at=s.updated_at.isoformat(),
            ),
            "messages": msgs,
        }

    @app.patch("/api/sessions/{session_id}", response_model=SessionOut)
    def rename_session(session_id: str, body: RenameBody) -> SessionOut:
        s = st.rename_session(session_id, body.title)
        if not s:
            raise HTTPException(404, f"session {session_id} not found")
        return SessionOut(
            id=s.id, title=s.title,
            created_at=s.created_at.isoformat(),
            updated_at=s.updated_at.isoformat(),
        )

    @app.delete("/api/sessions/{session_id}")
    def delete_session(session_id: str) -> dict:
        ok = st.delete_session(session_id)
        if not ok:
            raise HTTPException(404, f"session {session_id} not found")
        return {"deleted": session_id}

    # ── ask (batch + stream) ────────────────────────────────────────────

    def _load_history(session_id: str) -> list[Message]:
        """Convert stored messages → agent's Message format.

        We only round-trip user+assistant text, intentionally dropping prior
        tool_calls / tool_results: those are baked into the assistant's final
        answer text, and replaying them on a new turn would (a) need provider
        tool IDs we don't persist, (b) burn context for no benefit.
        """
        out: list[Message] = []
        for m in st.list_messages(session_id):
            if m.role not in ("user", "assistant"):
                continue
            out.append(Message(role=m.role, content=m.content))
        return out

    def _persist_user(session_id: str, query: str) -> None:
        st.append_message(session_id, "user", query)

    def _persist_final(session_id: str, query: str, answer: str,
                       tool_calls: list[dict], turns: int,
                       latency_ms: float, error: str | None) -> None:
        st.append_message(session_id, "assistant", answer, meta={
            "turns": turns,
            "tool_calls": tool_calls,
            "latency_total_ms": latency_ms,
            "error": error,
        })
        st.log_audit(
            "research_query",
            session_id=session_id,
            note=query[:200],
            payload={
                "query": query, "answer_chars": len(answer or ""),
                "turns": turns, "tools": [t["name"] for t in tool_calls],
                "latency_total_ms": latency_ms, "error": error,
            },
        )

    @app.post("/api/sessions/{session_id}/ask", response_model=AskResponse)
    def ask(session_id: str, body: AskBody) -> AskResponse:
        if not st.get_session(session_id):
            raise HTTPException(404, f"session {session_id} not found")
        _persist_user(session_id, body.query)
        history = _load_history(session_id)
        try:
            trace = ag.run(history)
        except Exception as e:
            raise HTTPException(500, f"agent failed: {type(e).__name__}: {e}") from e
        _persist_final(
            session_id, body.query, trace.final_answer, trace.tool_calls,
            trace.turns, trace.latency_total_ms, trace.error,
        )
        return AskResponse(**trace.to_dict())

    @app.post("/api/sessions/{session_id}/ask/stream")
    def ask_stream(session_id: str, body: AskBody) -> StreamingResponse:
        if not st.get_session(session_id):
            raise HTTPException(404, f"session {session_id} not found")
        _persist_user(session_id, body.query)
        history = _load_history(session_id)

        def gen():
            final_answer = ""
            tool_calls: list[dict] = []
            turns = 0
            latency = 0.0
            error: str | None = None
            try:
                for ev in ag.stream(history):
                    if ev["type"] == "final":
                        final_answer = ev["answer"]
                        turns = ev["turns"]
                        tool_calls = ev["tool_calls"]
                        latency = ev["latency_total_ms"]
                    elif ev["type"] == "error":
                        error = ev["message"]
                    yield f"event: {ev['type']}\ndata: {json.dumps(ev, ensure_ascii=False, default=str)}\n\n"
            except Exception as e:
                error = f"{type(e).__name__}: {e}"
                yield (
                    "event: error\n"
                    f"data: {json.dumps({'type': 'error', 'message': error}, ensure_ascii=False)}\n\n"
                )
            finally:
                _persist_final(
                    session_id, body.query,
                    final_answer or (f"agent failed: {error}" if error else ""),
                    tool_calls, turns, latency, error,
                )
                yield "event: done\ndata: {}\n\n"

        return StreamingResponse(
            gen(),
            media_type="text/event-stream",
            headers={"cache-control": "no-cache", "x-accel-buffering": "no"},
        )

    # ── audit ───────────────────────────────────────────────────────────

    @app.get("/api/audit")
    def list_audit(limit: int = 100, action: str | None = None) -> list[dict]:
        return [
            {
                "id": e.id, "ts": e.ts.isoformat(), "session_id": e.session_id,
                "action": e.action, "note": e.note,
                "payload": json.loads(e.payload_json) if e.payload_json else {},
            }
            for e in st.list_audit(limit=limit, action=action)
        ]

    return app


# Convenience singleton — `uvicorn faro_research.server.app:app`
app = make_app()
