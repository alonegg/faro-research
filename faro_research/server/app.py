"""FastAPI server — sessions, messages, streaming agent calls.

v0.4 endpoints:
    GET    /api/health
    GET    /api/tools
    GET    /api/auth/me                       — current user info
    POST   /api/auth/users                    — admin-only: mint user + key
    GET    /api/auth/users                    — admin-only: list
    GET    /api/sessions                      — list (per-user)
    POST   /api/sessions
    GET    /api/sessions/{id}
    PATCH  /api/sessions/{id}
    DELETE /api/sessions/{id}
    POST   /api/sessions/{id}/ask             — batch
    POST   /api/sessions/{id}/ask/stream      — SSE
    GET    /api/sessions/{id}/export.{md,pdf} — research report download
    GET    /api/audit                         — per-user

Auth model:
  - When `FARO_AUTH_REQUIRED=1` env: every request needs `Authorization: Bearer <api-key>`
  - Otherwise (v0.3-compat): all requests treated as the sentinel `default` user
  - Bootstrap admin via `FARO_ADMIN_KEY` env on cold start

Per-user state:
  - sessions / audit filter by user_id (column added in v0.4 migration)
  - memory store mounted at `data/memory/{user_id}/` (separate dirs)
  - agent + memory are cached per-user and lazily built on first hit

Customise via `make_app(registry=..., agent_factory=..., store=...)` from
your own bootstrap if you need to swap any layer.
"""

from __future__ import annotations

import json
import logging
import os

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field

from faro_research import __version__
from faro_research.agent import Agent, Message, build_system_prompt
from faro_research.audit import SessionStore
from faro_research.auth import User, UserStore
from faro_research.config import settings
from faro_research.memory import MemoryStore, make_memory_tools
from faro_research.providers import Provider, make_provider
from faro_research.skills import make_skill_tool
from faro_research.tools import ToolRegistry, discover_external_tools
from faro_research.tools.builtin.tushare import tushare_default_tools

log = logging.getLogger(__name__)


def _auth_required() -> bool:
    return os.getenv("FARO_AUTH_REQUIRED", "").strip() in ("1", "true", "yes")


# ────────────────────────────────────────────────────────────────────────────
# Per-user agent factory — caches one Agent per user (memory + tools live here)
# ────────────────────────────────────────────────────────────────────────────


class _UserAgentCache:
    """Lazy-built per-user agent + MemoryStore. Cached for the process lifetime
    (memory store has its own SQLite connection so re-use across requests is
    cheap and avoids file locking)."""

    def __init__(self, provider: Provider) -> None:
        self.provider = provider
        self._cache: dict[str, tuple[MemoryStore, ToolRegistry, Agent]] = {}

    def _build_registry(self, memory: MemoryStore) -> ToolRegistry:
        reg = ToolRegistry()
        reg.register_many(tushare_default_tools(self.provider))
        skill = make_skill_tool()
        if skill is not None:
            reg.register(skill)
        reg.register_many(make_memory_tools(memory))
        for spec in discover_external_tools():
            if spec.name in reg:
                continue
            reg.register(spec)
        return reg

    def get(self, user_id: str) -> tuple[MemoryStore, Agent]:
        if user_id not in self._cache:
            mem_root = settings.db_path.parent / "memory" / user_id
            memory = MemoryStore(root=mem_root)
            registry = self._build_registry(memory)
            sys_prompt = build_system_prompt(soul=memory.soul(), rules=memory.rules())
            agent = Agent(
                provider=self.provider, tools=registry, system_prompt=sys_prompt,
            )
            self._cache[user_id] = (memory, registry, agent)
        memory, _, agent = self._cache[user_id]
        return memory, agent


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


class UserOut(BaseModel):
    id: str
    email: str
    role: str
    created_at: str


class CreateUserBody(BaseModel):
    email: str = Field(..., min_length=3, max_length=200)
    role: str = Field(default="user", pattern="^(user|admin)$")


class CreateUserResponse(BaseModel):
    user: UserOut
    api_key: str   # plaintext, shown ONCE


# ────────────────────────────────────────────────────────────────────────────
# App factory
# ────────────────────────────────────────────────────────────────────────────


def make_app(
    *,
    store: SessionStore | None = None,
    user_store: UserStore | None = None,
) -> FastAPI:
    provider = make_provider()
    st = store or SessionStore()
    us = user_store or UserStore()
    cache = _UserAgentCache(provider)

    app = FastAPI(
        title="Faro Research",
        version=__version__,
        description="A-share research agent — pluggable tools + SSE streaming.",
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ── auth dependency ────────────────────────────────────────────────

    def current_user(authorization: str | None = Header(default=None)) -> User:
        """Resolve the request's user.

        - FARO_AUTH_REQUIRED unset: always returns the `default` user
        - Required: parses Bearer token; 401 on missing / invalid
        """
        if not _auth_required():
            return us.get_default()
        if not authorization or not authorization.lower().startswith("bearer "):
            raise HTTPException(401, "missing Bearer token in Authorization header")
        token = authorization.split(None, 1)[1].strip()
        user = us.lookup_by_key(token)
        if user is None:
            raise HTTPException(401, "invalid api key")
        return user

    def admin_user(user: User = Depends(current_user)) -> User:
        if user.role != "admin":
            raise HTTPException(403, "admin only")
        return user

    # ── meta ────────────────────────────────────────────────────────────

    @app.get("/api/health")
    def health() -> dict:
        return {
            "status": "ok",
            "version": __version__,
            "provider": provider.name,
            "auth_required": _auth_required(),
        }

    @app.get("/api/auth/me", response_model=UserOut)
    def me(user: User = Depends(current_user)) -> UserOut:
        return UserOut(
            id=user.id, email=user.email, role=user.role,
            created_at=user.created_at.isoformat(),
        )

    @app.get("/api/auth/users", response_model=list[UserOut])
    def list_users(_admin: User = Depends(admin_user)) -> list[UserOut]:
        return [
            UserOut(id=u.id, email=u.email, role=u.role,
                    created_at=u.created_at.isoformat())
            for u in us.list_users()
        ]

    @app.post("/api/auth/users", response_model=CreateUserResponse)
    def create_user(body: CreateUserBody,
                    _admin: User = Depends(admin_user)) -> CreateUserResponse:
        user, key = us.create_user(email=body.email, role=body.role)
        return CreateUserResponse(
            user=UserOut(
                id=user.id, email=user.email, role=user.role,
                created_at=user.created_at.isoformat(),
            ),
            api_key=key,
        )

    @app.get("/api/tools", response_model=list[ToolInfo])
    def list_tools(user: User = Depends(current_user)) -> list[ToolInfo]:
        # Build the user's registry (cheap if cached)
        _, agent = cache.get(user.id)
        return [
            ToolInfo(name=s.name, description=s.description, parameters=s.parameters)
            for s in agent.tools.specs()
        ]

    # ── sessions ────────────────────────────────────────────────────────

    @app.get("/api/sessions", response_model=list[SessionOut])
    def list_sessions(user: User = Depends(current_user)) -> list[SessionOut]:
        return [
            SessionOut(
                id=s.id, title=s.title,
                created_at=s.created_at.isoformat(),
                updated_at=s.updated_at.isoformat(),
            )
            for s in st.list_sessions(user_id=user.id)
        ]

    @app.post("/api/sessions", response_model=SessionOut)
    def create_session(body: CreateSessionBody,
                       user: User = Depends(current_user)) -> SessionOut:
        s = st.create_session(title=body.title, user_id=user.id)
        return SessionOut(
            id=s.id, title=s.title,
            created_at=s.created_at.isoformat(),
            updated_at=s.updated_at.isoformat(),
        )

    @app.get("/api/sessions/{session_id}")
    def get_session(session_id: str,
                    user: User = Depends(current_user)) -> dict:
        s = st.get_session(session_id, user_id=user.id)
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
    def rename_session(session_id: str, body: RenameBody,
                       user: User = Depends(current_user)) -> SessionOut:
        if not st.get_session(session_id, user_id=user.id):
            raise HTTPException(404, f"session {session_id} not found")
        s = st.rename_session(session_id, body.title)
        return SessionOut(
            id=s.id, title=s.title,
            created_at=s.created_at.isoformat(),
            updated_at=s.updated_at.isoformat(),
        )

    @app.delete("/api/sessions/{session_id}")
    def delete_session(session_id: str,
                       user: User = Depends(current_user)) -> dict:
        if not st.get_session(session_id, user_id=user.id):
            raise HTTPException(404, f"session {session_id} not found")
        st.delete_session(session_id)
        return {"deleted": session_id}

    # ── ask (batch + stream) ────────────────────────────────────────────

    def _load_history(session_id: str) -> list[Message]:
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
                       latency_ms: float, error: str | None,
                       user_id: str) -> None:
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
            user_id=user_id,
        )

    @app.post("/api/sessions/{session_id}/ask", response_model=AskResponse)
    def ask(session_id: str, body: AskBody,
            user: User = Depends(current_user)) -> AskResponse:
        if not st.get_session(session_id, user_id=user.id):
            raise HTTPException(404, f"session {session_id} not found")
        _persist_user(session_id, body.query)
        history = _load_history(session_id)
        _, agent = cache.get(user.id)
        try:
            trace = agent.run(history)
        except Exception as e:
            raise HTTPException(500, f"agent failed: {type(e).__name__}: {e}") from e
        _persist_final(
            session_id, body.query, trace.final_answer, trace.tool_calls,
            trace.turns, trace.latency_total_ms, trace.error, user.id,
        )
        return AskResponse(**trace.to_dict())

    @app.post("/api/sessions/{session_id}/ask/stream")
    def ask_stream(session_id: str, body: AskBody,
                   user: User = Depends(current_user)) -> StreamingResponse:
        if not st.get_session(session_id, user_id=user.id):
            raise HTTPException(404, f"session {session_id} not found")
        _persist_user(session_id, body.query)
        history = _load_history(session_id)
        _, agent = cache.get(user.id)

        def gen():
            final_answer = ""
            tool_calls: list[dict] = []
            turns = 0
            latency = 0.0
            error: str | None = None
            try:
                for ev in agent.stream(history):
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
                    tool_calls, turns, latency, error, user.id,
                )
                yield "event: done\ndata: {}\n\n"

        return StreamingResponse(
            gen(),
            media_type="text/event-stream",
            headers={"cache-control": "no-cache", "x-accel-buffering": "no"},
        )

    # ── export (md + pdf) ──────────────────────────────────────────────

    @app.get("/api/sessions/{session_id}/export.{fmt}")
    def export_session(
        session_id: str, fmt: str,
        user: User = Depends(current_user),
    ):
        from urllib.parse import quote

        from faro_research.export import markdown_to_pdf, session_to_markdown
        if not st.get_session(session_id, user_id=user.id):
            raise HTTPException(404, f"session {session_id} not found")
        s = st.get_session(session_id)
        msgs = st.list_messages(session_id)
        md = session_to_markdown(s, msgs)

        # Filename: ASCII-safe fallback + RFC 5987 (UTF-8) extended form for
        # Chinese titles. Browsers prefer the UTF-8 form when present.
        title = s.title or "session"
        ascii_safe = "".join(
            c if c.isascii() and (c.isalnum() or c in "-_") else "_"
            for c in title
        )[:40].strip("_") or session_id
        utf8_quoted = quote(title, safe="")
        cd = (f'attachment; filename="{ascii_safe}.{fmt}"; '
              f"filename*=UTF-8''{utf8_quoted}.{fmt}")

        if fmt == "md":
            return Response(
                content=md, media_type="text/markdown; charset=utf-8",
                headers={"content-disposition": cd},
            )
        if fmt == "pdf":
            try:
                pdf_bytes = markdown_to_pdf(md, title=s.title)
            except RuntimeError as e:
                raise HTTPException(500, str(e)) from e
            return Response(
                content=pdf_bytes, media_type="application/pdf",
                headers={"content-disposition": cd},
            )
        raise HTTPException(400, f"unsupported format: {fmt!r} (use md or pdf)")

    # ── audit ───────────────────────────────────────────────────────────

    @app.get("/api/audit")
    def list_audit(limit: int = 100, action: str | None = None,
                   user: User = Depends(current_user)) -> list[dict]:
        return [
            {
                "id": e.id, "ts": e.ts.isoformat(), "session_id": e.session_id,
                "action": e.action, "note": e.note,
                "payload": json.loads(e.payload_json) if e.payload_json else {},
            }
            for e in st.list_audit(limit=limit, action=action, user_id=user.id)
        ]

    return app


# Convenience singleton — `uvicorn faro_research.server.app:app`
app = make_app()
