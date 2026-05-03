"""User model + API key storage.

Auth model (v0.4):
  - Each user has 1 hashed API key (sha256, salted with project secret)
  - Bearer token in Authorization header → looked up by hash
  - Single FARO_ADMIN_KEY env var bootstraps the first admin on cold start

Why not OAuth/OIDC for v0.4: API keys cover 95% of self-hosted use cases
(SaaS, internal tools), have zero browser-redirect complexity, and work
identically for CLI, server, and frontend. OIDC ships in v0.5.

Why not bcrypt/argon2: API keys are high-entropy random tokens (32+ bytes),
not user-typed passwords. SHA-256 + a 32-byte secret is sufficient and
keeps the dep tree tiny.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import hmac
import logging
import os
import secrets
from datetime import datetime
from pathlib import Path

from sqlmodel import Field, Session, SQLModel, create_engine, select

from faro_research.config import settings

log = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(_dt.UTC).replace(tzinfo=None)


def _project_secret() -> bytes:
    """Pepper used when hashing API keys.

    Read from FARO_AUTH_SECRET env var; falls back to a deterministic
    derivation from the DB path so single-machine installs don't lose
    auth on restart. For multi-machine deployments, set the env var.
    """
    s = os.getenv("FARO_AUTH_SECRET", "").strip()
    if s:
        return s.encode("utf-8")
    return f"faro:{settings.db_path}".encode()


def hash_api_key(key: str) -> str:
    """HMAC-SHA256(secret, key) → hex digest. Constant time vs naive hash."""
    return hmac.new(_project_secret(), key.encode("utf-8"), hashlib.sha256).hexdigest()


def generate_api_key() -> str:
    """High-entropy URL-safe token. Show to user once; store only the hash."""
    return "fr-" + secrets.token_urlsafe(32)


# ────────────────────────────────────────────────────────────────────────────
# ORM
# ────────────────────────────────────────────────────────────────────────────


class User(SQLModel, table=True):
    __tablename__ = "user"

    id: str = Field(primary_key=True, max_length=32)
    email: str = Field(unique=True, index=True, max_length=200)
    api_key_hash: str = Field(unique=True, index=True, max_length=64)
    role: str = Field(default="user", max_length=16)   # user | admin
    created_at: datetime = Field(default_factory=_utcnow)
    last_seen_at: datetime | None = None


# Sentinel "default" user for auth-disabled (v0.3-compatible) deployments
DEFAULT_USER_ID = "default"
DEFAULT_USER_EMAIL = "default@local"


# ────────────────────────────────────────────────────────────────────────────
# Store
# ────────────────────────────────────────────────────────────────────────────


class UserStore:
    """SQLite-backed user store. Reuses the same DB file as SessionStore."""

    def __init__(self, db_path: Path | None = None) -> None:
        path = db_path or settings.db_path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.engine = create_engine(
            f"sqlite:///{path}",
            echo=False,
            connect_args={"check_same_thread": False},
        )
        SQLModel.metadata.create_all(self.engine)
        self._bootstrap_default()
        self._bootstrap_admin()

    # ── bootstrap ───────────────────────────────────────────────────────

    def _bootstrap_default(self) -> None:
        """Ensure the sentinel `default` user exists.

        Used when FARO_AUTH_REQUIRED is unset (v0.3-compat single-tenant mode);
        also acts as the migration target for any pre-v0.4 sessions / memory.
        """
        with Session(self.engine) as db:
            if db.get(User, DEFAULT_USER_ID):
                return
            db.add(User(
                id=DEFAULT_USER_ID,
                email=DEFAULT_USER_EMAIL,
                api_key_hash=hash_api_key("(unused — auth disabled)"),
                role="admin",
            ))
            db.commit()

    def _bootstrap_admin(self) -> None:
        """Promote the FARO_ADMIN_KEY holder to admin on every start.

        Idempotent: if the user already exists, just update the role.
        """
        admin_key = os.getenv("FARO_ADMIN_KEY", "").strip()
        if not admin_key:
            return
        h = hash_api_key(admin_key)
        with Session(self.engine) as db:
            existing = db.exec(select(User).where(User.api_key_hash == h)).first()
            if existing:
                if existing.role != "admin":
                    existing.role = "admin"
                    db.add(existing)
                    db.commit()
                return
            # Fresh admin — generate a synthetic email
            uid = f"u-{secrets.token_hex(6)}"
            db.add(User(
                id=uid,
                email="admin@local",
                api_key_hash=h,
                role="admin",
            ))
            db.commit()
            log.info("bootstrap admin user %s from FARO_ADMIN_KEY env", uid)

    # ── public ops ──────────────────────────────────────────────────────

    def create_user(self, email: str, role: str = "user") -> tuple[User, str]:
        """Mint a new user + plaintext API key. Caller must show the key
        ONCE to the user — it's not recoverable from the DB after this."""
        api_key = generate_api_key()
        user = User(
            id=f"u-{secrets.token_hex(6)}",
            email=email.strip(),
            api_key_hash=hash_api_key(api_key),
            role=role,
        )
        with Session(self.engine) as db:
            db.add(user)
            db.commit()
            db.refresh(user)
        return user, api_key

    def lookup_by_key(self, key: str) -> User | None:
        if not key:
            return None
        h = hash_api_key(key)
        with Session(self.engine) as db:
            user = db.exec(select(User).where(User.api_key_hash == h)).first()
            if user:
                user.last_seen_at = _utcnow()
                db.add(user)
                db.commit()
                db.refresh(user)
            return user

    def get(self, user_id: str) -> User | None:
        with Session(self.engine) as db:
            return db.get(User, user_id)

    def list_users(self) -> list[User]:
        with Session(self.engine) as db:
            return db.exec(select(User).order_by(User.created_at.desc())).all()

    def get_default(self) -> User:
        u = self.get(DEFAULT_USER_ID)
        if u is None:
            raise RuntimeError("default user missing — _bootstrap_default never ran?")
        return u
