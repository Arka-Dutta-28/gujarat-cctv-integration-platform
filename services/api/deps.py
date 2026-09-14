"""Shared request dependencies."""

from __future__ import annotations

from typing import Annotated

from fastapi import Header

__all__ = ["Actor", "CaseRef"]


def _actor(x_actor: Annotated[str | None, Header(alias="X-Actor")] = None) -> str:
    """Who is performing this action, for the audit trail.

    Until authentication lands in M8 this comes from a header and defaults to
    `anonymous`. The audit trail is therefore already wired end to end; M8
    replaces this one function with the authenticated principal and every
    existing call site starts recording real identities.
    """
    return (x_actor or "anonymous").strip()[:120]


def _case_ref(
    x_case_ref: Annotated[str | None, Header(alias="X-Case-Ref")] = None,
) -> str | None:
    """Investigation reference, for DPDP purpose-binding.

    Access to personal data is supposed to be bound to a stated purpose. Carrying
    the case reference on the request means the audit row records *why* a plate
    was searched, not merely that it was.
    """
    return (x_case_ref or "").strip()[:120] or None


Actor = Annotated[str, Header()]
CaseRef = Annotated[str | None, Header()]
