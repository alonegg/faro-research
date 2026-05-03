"""SQLite-backed multi-session store.

Three tables:
  - session     : one row per chat (id, title, created_at, updated_at)
  - message     : append-only conversation history
  - audit_event : tool calls, errors, latencies (one row per agent run)

v0.1 is single-tenant (no user_id column). v0.2 will add user_id when the
auth layer lands.
"""

from __future__ import annotations

import datetime as _dt
import json
import secrets
from datetime import datetime
from pathlib import Path

from sqlmodel import Field, Session, SQLModel, create_engine, select

from faro_research.config import settings


def _utcnow() -> datetime:
    return datetime.now(_dt.UTC).replace(tzinfo=None)


def _new_id(prefix: str) -> str:
    return f"{prefix}-{secrets.token_hex(6)}"


# ────────────────────────────────────────────────────────────────────────────
# ORM
# ────────────────────────────────────────────────────────────────────────────


class ChatSession(SQLModel, table=True):
    __tablename__ = "session"

    id: str = Field(primary_key=True, max_length=32)
    title: str = Field(default="新会话", max_length=200)
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)


class ChatMessage(SQLModel, table=True):
    __tablename__ = "message"

    id: int | None = Field(default=None, primary_key=True)
    session_id: str = Field(foreign_key="session.id", index=True, max_length=32)
    seq: int                                   # 0-based ordering within session
    role: str = Field(max_length=16)           # system | user | assistant | tool
    content: str = ""
    # JSON-serialised extras: tool_calls (assistant), tool_call_id (tool),
    # name (tool), reasoning_content (provider echo)
    meta_json: str = "{}"
    created_at: datetime = Field(default_factory=_utcnow)


class AuditEvent(SQLModel, table=True):
    __tablename__ = "audit_event"

    id: int | None = Field(default=None, primary_key=True)
    ts: datetime = Field(default_factory=_utcnow, index=True)
    session_id: str | None = Field(default=None, index=True, max_length=32)
    action: str = Field(max_length=64, index=True)   # research_query | tool_call | error
    note: str | None = None
    payload_json: str = "{}"


# ────────────────────────────────────────────────────────────────────────────
# Store
# ────────────────────────────────────────────────────────────────────────────


class SessionStore:
    """Thin sync wrapper around the engine. Caller responsible for using
    short-lived `Session` instances (one per request)."""

    def __init__(self, db_path: Path | None = None) -> None:
        path = db_path or settings.db_path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.engine = create_engine(
            f"sqlite:///{path}",
            echo=False,
            connect_args={"check_same_thread": False},
        )
        SQLModel.metadata.create_all(self.engine)

    # ── sessions ────────────────────────────────────────────────────────

    def create_session(self, title: str | None = None) -> ChatSession:
        s = ChatSession(id=_new_id("s"), title=title or "新会话")
        with Session(self.engine) as db:
            db.add(s)
            db.commit()
            db.refresh(s)
        return s

    def list_sessions(self, limit: int = 50) -> list[ChatSession]:
        with Session(self.engine) as db:
            return db.exec(
                select(ChatSession).order_by(ChatSession.updated_at.desc()).limit(limit)
            ).all()

    def get_session(self, session_id: str) -> ChatSession | None:
        with Session(self.engine) as db:
            return db.get(ChatSession, session_id)

    def delete_session(self, session_id: str) -> bool:
        with Session(self.engine) as db:
            s = db.get(ChatSession, session_id)
            if not s:
                return False
            for m in db.exec(select(ChatMessage).where(ChatMessage.session_id == session_id)).all():
                db.delete(m)
            db.delete(s)
            db.commit()
            return True

    def rename_session(self, session_id: str, title: str) -> ChatSession | None:
        with Session(self.engine) as db:
            s = db.get(ChatSession, session_id)
            if not s:
                return None
            s.title = title[:200]
            s.updated_at = _utcnow()
            db.add(s)
            db.commit()
            db.refresh(s)
        return s

    def touch_session(self, session_id: str) -> None:
        with Session(self.engine) as db:
            s = db.get(ChatSession, session_id)
            if s:
                s.updated_at = _utcnow()
                db.add(s)
                db.commit()

    # ── messages ────────────────────────────────────────────────────────

    def list_messages(self, session_id: str) -> list[ChatMessage]:
        with Session(self.engine) as db:
            return db.exec(
                select(ChatMessage)
                .where(ChatMessage.session_id == session_id)
                .order_by(ChatMessage.seq.asc())
            ).all()

    def append_message(
        self,
        session_id: str,
        role: str,
        content: str,
        *,
        meta: dict | None = None,
    ) -> ChatMessage:
        with Session(self.engine) as db:
            existing = db.exec(
                select(ChatMessage).where(ChatMessage.session_id == session_id)
            ).all()
            seq = len(existing)
            msg = ChatMessage(
                session_id=session_id,
                seq=seq,
                role=role,
                content=content or "",
                meta_json=json.dumps(meta or {}, ensure_ascii=False, default=str),
            )
            db.add(msg)
            # bump session updated_at
            s = db.get(ChatSession, session_id)
            if s:
                s.updated_at = _utcnow()
                db.add(s)
            db.commit()
            db.refresh(msg)
        return msg

    # ── audit ───────────────────────────────────────────────────────────

    def log_audit(
        self,
        action: str,
        *,
        session_id: str | None = None,
        note: str | None = None,
        payload: dict | None = None,
    ) -> None:
        try:
            with Session(self.engine) as db:
                db.add(AuditEvent(
                    session_id=session_id,
                    action=action,
                    note=(note or "")[:500],
                    payload_json=json.dumps(payload or {}, ensure_ascii=False, default=str),
                ))
                db.commit()
        except Exception:
            # Audit write must never fail the agent run
            pass

    def list_audit(self, limit: int = 100, action: str | None = None) -> list[AuditEvent]:
        with Session(self.engine) as db:
            q = select(AuditEvent).order_by(AuditEvent.ts.desc()).limit(limit)
            if action:
                q = q.where(AuditEvent.action == action)
            return db.exec(q).all()
