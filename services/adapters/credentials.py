"""Credential resolution.

`cameras.credential_ref` holds a *key*, never a secret. This module turns that
key into a username and password at connect time, from the environment.

The indirection is the point (invariant 5). A password that only exists in the
process environment cannot leak through a database dump, an API response, a
GeoJSON property, an audit row or a log line — all of which carry `stream_ref`
and `credential_ref` freely.

Environment convention, for `credential_ref = "junagadh_ptz"`:

    CAMERA_CRED_JUNAGADH_PTZ_USER=operator
    CAMERA_CRED_JUNAGADH_PTZ_PASSWORD=...

A hosted deployment swaps this module's backend for a real secret manager
without any adapter changing.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from urllib.parse import quote, urlsplit, urlunsplit

__all__ = ["Credential", "resolve", "apply_to_url", "redact"]

_SAFE_KEY = re.compile(r"[^A-Z0-9_]")


@dataclass(frozen=True)
class Credential:
    username: str
    password: str

    def __repr__(self) -> str:
        # Defensive: this object ends up inside exception messages and log
        # records, and a dataclass would otherwise print the password.
        return f"Credential(username={self.username!r}, password=***)"


def _env_key(credential_ref: str) -> str:
    return _SAFE_KEY.sub("_", credential_ref.strip().upper())


def resolve(credential_ref: str | None) -> Credential | None:
    """Look up a credential by reference. None when unset or not configured."""
    if not credential_ref:
        return None
    key = _env_key(credential_ref)
    user = os.environ.get(f"CAMERA_CRED_{key}_USER")
    password = os.environ.get(f"CAMERA_CRED_{key}_PASSWORD")
    if user is None and password is None:
        return None
    return Credential(username=user or "", password=password or "")


def apply_to_url(url: str, credential: Credential | None) -> str:
    """Inject credentials into a URL's authority, for handing to ffmpeg.

    The result is a secret. It must go straight to the process that needs it and
    must never be returned to a client, stored, or logged — use `redact` for
    anything that will be seen.
    """
    if credential is None:
        return url
    parts = urlsplit(url)
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    userinfo = f"{quote(credential.username, safe='')}:{quote(credential.password, safe='')}"
    return urlunsplit(
        (parts.scheme, f"{userinfo}@{host}", parts.path, parts.query, parts.fragment)
    )


def redact(url: str) -> str:
    """Strip any userinfo from a URL so it is safe to log or return."""
    parts = urlsplit(url)
    if not parts.username and not parts.password:
        return url
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    return urlunsplit((parts.scheme, f"***@{host}", parts.path, parts.query, parts.fragment))
