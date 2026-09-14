"""Audit trail, readable.

An audit log nobody can read is only half a control: it proves nothing to an
evaluator, and it cannot answer the question a supervisor actually asks — *who
looked at this camera, and under what case reference?*

Read-only by design. There is no endpoint that edits or deletes an entry, and
there should never be one: an append-only trail that the application itself
cannot rewrite is the property that makes it evidence.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from services.common.db import fetch_all, fetch_one

router = APIRouter(prefix="/api/audit", tags=["audit"])


class AuditEntry(BaseModel):
    id: int
    ts: str
    actor: str
    action: str = Field(description="Canonical action, e.g. `stream.view`, `camera.create`.")
    subject: str | None = Field(
        default=None, description="What was acted on — usually a camera id."
    )
    case_ref: str | None = Field(
        default=None,
        description="Investigation this access was bound to. Absent means unbound access.",
    )
    detail: dict[str, Any] | None = None


@router.get(
    "",
    response_model=list[AuditEntry],
    summary="Read the audit trail",
    description=(
        "Newest first. Filter by `actor`, `action`, `subject` or `case_ref`.\n\n"
        "Every camera mutation, stream view, plate search, journey query and "
        "report export appears here. `case_ref` is the DPDP purpose-binding "
        "record: it says *why* personal data was accessed, not just that it was."
    ),
)
def list_audit(
    actor: Annotated[str | None, Query(description="Exact actor match.")] = None,
    action: Annotated[str | None, Query(description="Exact action match.")] = None,
    subject: Annotated[str | None, Query(description="Exact subject match.")] = None,
    case_ref: Annotated[str | None, Query(description="Exact case reference match.")] = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
) -> list[AuditEntry]:
    # Built as a filter list rather than string concatenation: every value stays
    # a bound parameter, so a case reference typed by an operator can never
    # become SQL.
    where, params = [], {}
    for column, value in (
        ("actor", actor), ("action", action), ("subject", subject), ("case_ref", case_ref)
    ):
        if value is not None:
            where.append(f"{column} = %({column})s")
            params[column] = value
    params["limit"] = limit

    rows = fetch_all(
        "SELECT id, ts, actor, action, subject, case_ref, detail FROM audit_log"
        + (" WHERE " + " AND ".join(where) if where else "")
        + " ORDER BY ts DESC, id DESC LIMIT %(limit)s",
        params,
    )
    return [AuditEntry(**{**r, "ts": r["ts"].isoformat()}) for r in rows]


@router.get(
    "/summary",
    summary="Audit activity by action",
    description=(
        "Counts per action, with how many carried a case reference. A high "
        "proportion of unbound accesses is itself the finding."
    ),
)
def audit_summary() -> dict[str, Any]:
    rows = fetch_all(
        "SELECT action, count(*) AS n, count(case_ref) AS with_case_ref,"
        " count(DISTINCT actor) AS actors, max(ts) AS latest"
        " FROM audit_log GROUP BY action ORDER BY n DESC"
    )
    total = fetch_one("SELECT count(*) AS n, count(case_ref) AS with_case_ref FROM audit_log")
    return {
        "total": (total or {}).get("n", 0),
        "with_case_ref": (total or {}).get("with_case_ref", 0),
        "by_action": [
            {**r, "latest": r["latest"].isoformat() if r["latest"] else None} for r in rows
        ],
    }
