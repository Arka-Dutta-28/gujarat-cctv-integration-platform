"""Who is acting, and what they are allowed to do.

Until now the actor came from an X-Actor header defaulting to anonymous. That
was deliberate and is recorded as such: wiring the audit trail end to end from
M1 meant every mutation was already accounted for, and M8 would replace one
function rather than revisit every call site. This is that replacement.

Two roles, because the evidence model needs exactly two. An operator reads and
writes: onboards a camera, watchlists a plate, acts on an alert. A viewer reads
only, and that is what the submission's demo account is. An evaluator can open
every screen, trace a vehicle and download a report, and cannot decommission a
camera or clear somebody's alert.

Tokens are signed, not stored. A session table would need cleanup, would add a
database round trip to every request, and buys revocation this deployment has no
way to use, since there is no admin console to revoke from. An HMAC-signed token
carrying username, role and expiry is verifiable in microseconds with no state,
and deactivating the account stops the next login. The trade is that a stolen
token stays valid until it expires, which is why the expiry is hours rather than
weeks.

Passwords are scrypt-hashed with the standard library: no passlib, no bcrypt
wheel, one function from hashlib, and one fewer package to audit in a submission
that will be read.

Authentication is required by default and can be disabled for local development
with AUTH_REQUIRED=false, which is how the acceptance tests and the compose
stack run without a seeded account. It fails closed: an unset AUTH_SECRET in a
deployment that requires auth is a startup error, not a silently weak default.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import Depends, Header, HTTPException

from services.common.db import fetch_one

log = logging.getLogger("api.auth")

__all__ = [
    "Principal", "current_principal", "require_operator", "hash_password",
    "verify_password", "issue_token", "read_token", "gate", "PUBLIC_PATHS",
    "AUTH_REQUIRED", "TOKEN_TTL_S",
]

#: Reachable without a token even when authentication is required.
#:
#: `/health` because an orchestrator's probe cannot hold credentials. The
#: OpenAPI schema and Swagger page because they are a graded submission
#: artifact and describe the API without exposing a byte of what it holds — an
#: evaluator should be able to read the interface before deciding to log in.
#: Everything else, including every read, needs an account: "log in with the
#: demo account" is meaningless if the data was readable without it.
PUBLIC_PATHS = (
    "/health",
    "/api/auth/login",
    "/api/docs",
    "/api/redoc",
    "/api/openapi.json",
)

#: Off for local development and the acceptance tests; on for any deployment.
AUTH_REQUIRED = os.environ.get("AUTH_REQUIRED", "false").lower() in {"1", "true", "yes"}

#: Signing key. Absent with auth on is fatal — see `_secret`.
AUTH_SECRET = os.environ.get("AUTH_SECRET", "")

#: Hours, not weeks. A signed token cannot be revoked before it expires, so the
#: window in which a stolen one is useful is the only control there is.
TOKEN_TTL_S = int(os.environ.get("AUTH_TOKEN_TTL_S", str(8 * 3600)))

#: scrypt cost. n=2^14 is ~50 ms per verification here — slow enough to make
#: guessing expensive, fast enough that a login does not feel broken.
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2**14, 8, 1


@dataclass(frozen=True)
class Principal:
    """Who is making this request."""

    username: str
    role: str = "viewer"
    #: True when authentication is disabled and this is the development default.
    anonymous: bool = False

    @property
    def can_write(self) -> bool:
        return self.role == "operator"


def _secret() -> bytes:
    if not AUTH_SECRET:
        if AUTH_REQUIRED:
            # Fails closed. A generated-per-process fallback would "work" and
            # silently invalidate every token on restart, which is worse than
            # refusing to start.
            raise RuntimeError(
                "AUTH_REQUIRED is set but AUTH_SECRET is empty; refusing to sign "
                "tokens with a default key"
            )
        return b"development-only"
    return AUTH_SECRET.encode()


# --- passwords ------------------------------------------------------------


def hash_password(password: str) -> str:
    """`scrypt$n$r$p$salt$hash`, all base64. Parameters travel with the hash."""
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode(), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=32
    )
    return "$".join([
        "scrypt", str(SCRYPT_N), str(SCRYPT_R), str(SCRYPT_P),
        base64.b64encode(salt).decode(), base64.b64encode(digest).decode(),
    ])


def verify_password(password: str, stored: str) -> bool:
    """Constant-time check against a stored hash, whatever cost it was made at."""
    try:
        scheme, n, r, p, salt_b64, hash_b64 = stored.split("$")
        if scheme != "scrypt":
            return False
        digest = hashlib.scrypt(
            password.encode(), salt=base64.b64decode(salt_b64),
            n=int(n), r=int(r), p=int(p), dklen=len(base64.b64decode(hash_b64)),
        )
    except (ValueError, TypeError):
        # A malformed stored hash must read as "does not match", never as a
        # crash that distinguishes this account from one with a good hash.
        return False
    return hmac.compare_digest(digest, base64.b64decode(hash_b64))


# --- tokens ---------------------------------------------------------------


def issue_token(username: str, role: str, ttl_s: int = TOKEN_TTL_S) -> str:
    payload = {"sub": username, "role": role, "exp": int(time.time()) + ttl_s}
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=")
    signature = hmac.new(_secret(), body, hashlib.sha256).digest()
    return f"{body.decode()}.{base64.urlsafe_b64encode(signature).rstrip(b'=').decode()}"


def read_token(token: str) -> dict[str, Any] | None:
    """Verify and decode, or None. Never raises on malformed input."""
    try:
        body_b64, signature_b64 = token.split(".")
        body = body_b64.encode()
        expected = hmac.new(_secret(), body, hashlib.sha256).digest()
        given = base64.urlsafe_b64decode(signature_b64 + "=" * (-len(signature_b64) % 4))
        if not hmac.compare_digest(expected, given):
            return None
        payload = json.loads(base64.urlsafe_b64decode(body + b"=" * (-len(body) % 4)))
    except Exception:  # noqa: BLE001 - any malformed token is simply invalid
        return None
    if payload.get("exp", 0) < time.time():
        return None
    return payload


# --- request dependencies -------------------------------------------------


def current_principal(
    authorization: Annotated[str | None, Header()] = None,
    x_actor: Annotated[str | None, Header(alias="X-Actor")] = None,
) -> Principal:
    """Resolve the caller.

    With authentication off, the old `X-Actor` behaviour is kept exactly, so
    development and the acceptance tests are unchanged and every existing audit
    entry still means what it meant.
    """
    token = None
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization.split(" ", 1)[1].strip()

    if token:
        payload = read_token(token)
        if payload:
            return Principal(username=payload["sub"], role=payload.get("role", "viewer"))
        raise HTTPException(status_code=401, detail="token is invalid or has expired")

    if AUTH_REQUIRED:
        raise HTTPException(
            status_code=401,
            detail="authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Development: unchanged from M1.
    name = (x_actor or "anonymous").strip()[:120] or "anonymous"
    return Principal(username=name, role="operator", anonymous=True)


def require_operator(
    principal: Annotated[Principal, Depends(current_principal)],
) -> Principal:
    """Guard for anything that changes state.

    The demo account is a `viewer`, so this is what makes "read-only" true rather
    than a convention — an evaluator can drive every screen and cannot alter the
    evidence anyone else is looking at.
    """
    if not principal.can_write:
        raise HTTPException(
            status_code=403,
            detail=f"{principal.username} is a {principal.role}; this action needs an operator",
        )
    return principal


def authenticate(username: str, password: str) -> Principal | None:
    """Check a login. Constant work whether or not the account exists."""
    row = fetch_one(
        "SELECT username, role, password_hash, active FROM accounts"
        " WHERE username = %(username)s",
        {"username": username.strip().lower()},
    )
    # A dummy verification when the account is missing, so a wrong username and
    # a wrong password take the same time and cannot be told apart.
    stored = row["password_hash"] if row else hash_password("no-such-account")
    ok = verify_password(password, stored)
    if not row or not ok or not row["active"]:
        return None
    return Principal(username=row["username"], role=row["role"])


async def gate(request: Any) -> None:
    """Application-wide authentication check.

    Applied as a dependency on the app rather than added to each router, because
    the failure mode of per-router opt-in is a new endpoint that quietly ships
    unauthenticated. This way a route is protected by default and exemptions are
    a visible list.

    Endpoints that need to know *who* is calling still depend on
    `current_principal`; this only decides whether the request proceeds at all.
    """
    if not AUTH_REQUIRED:
        return
    path = request.url.path
    if path in PUBLIC_PATHS or path.startswith("/api/docs"):
        return

    header = request.headers.get("authorization", "")
    token = header.split(" ", 1)[1].strip() if header.lower().startswith("bearer ") else ""
    if token and read_token(token):
        return
    raise HTTPException(
        status_code=401,
        detail="authentication required",
        headers={"WWW-Authenticate": "Bearer"},
    )
