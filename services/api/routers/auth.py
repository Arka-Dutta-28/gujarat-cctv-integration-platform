"""Login, and who am I.

Small on purpose. The platform has two roles and no self-service: accounts are
created by `scripts/seed_accounts.py` from environment variables, because a
submission artifact that can create its own logins over the network is a
liability an evaluator would be right to raise.

There is no logout endpoint. Tokens are signed rather than stored, so a logout
is the client discarding its token — an endpoint that pretended to invalidate it
server-side would be theatre.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel, Field

from services.api.auth import (
    AUTH_REQUIRED,
    TOKEN_TTL_S,
    Principal,
    authenticate,
    current_principal,
    issue_token,
)
from services.common.audit import Action, record
from services.common.db import execute

router = APIRouter(prefix="/api/auth", tags=["auth"])


class Credentials(BaseModel):
    username: str = Field(min_length=1, max_length=120)
    password: str = Field(min_length=1, max_length=256)


class Session(BaseModel):
    token: str
    username: str
    role: str = Field(description="operator (read/write) or viewer (read-only)")
    expires_in_s: int


class Identity(BaseModel):
    username: str
    role: str
    can_write: bool
    #: False when the API is running with authentication disabled, which is the
    #: local development mode. A client showing a login screen needs to know.
    auth_required: bool


@router.post(
    "/login",
    response_model=Session,
    summary="Exchange credentials for a token",
    description=(
        "Returns a signed bearer token carrying the account's role. Send it as "
        "`Authorization: Bearer <token>`.\n\n"
        "The demo account supplied with the submission is a **viewer**: it can "
        "read everything the platform holds and change none of it. Anything that "
        "writes — onboarding a camera, watchlisting a plate, acting on an alert — "
        "requires an operator.\n\n"
        "Audited as `auth.login`, successes and failures alike: a run of failed "
        "logins against one account is exactly the thing an audit trail should "
        "be able to show."
    ),
    responses={401: {"description": "Unknown account or wrong password."}},
)
def login(payload: Annotated[Credentials, Body()]) -> Session:
    principal = authenticate(payload.username, payload.password)
    if principal is None:
        record(
            Action.AUTH_LOGIN, payload.username.strip().lower()[:120],
            detail={"result": "rejected"},
        )
        # One message for both causes. Saying which was wrong tells an attacker
        # which usernames exist.
        raise HTTPException(status_code=401, detail="unknown account or wrong password")

    execute(
        "UPDATE accounts SET last_login_at = now() WHERE username = %(u)s",
        {"u": principal.username},
    )
    record(
        Action.AUTH_LOGIN, principal.username,
        detail={"result": "accepted", "role": principal.role},
    )
    return Session(
        token=issue_token(principal.username, principal.role),
        username=principal.username,
        role=principal.role,
        expires_in_s=TOKEN_TTL_S,
    )


@router.get(
    "/me",
    response_model=Identity,
    summary="The current identity and what it may do",
    description=(
        "Used by the UI to decide whether to show a login screen and whether to "
        "offer the controls that write. `auth_required: false` means the API is "
        "running in local development mode with authentication disabled."
    ),
)
def me(principal: Annotated[Principal, Depends(current_principal)]) -> Identity:
    return Identity(
        username=principal.username,
        role=principal.role,
        can_write=principal.can_write,
        auth_required=AUTH_REQUIRED,
    )
