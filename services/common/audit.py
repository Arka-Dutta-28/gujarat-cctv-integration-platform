"""Audit trail.

Every stream view, plate search, journey query and export is recorded. This is
cheap to add now and painful to retrofit, and it carries two things the
submission needs: the security narrative, and DPDP purpose-binding — a
`case_ref` on a query is the record of *why* personal data was accessed.

Writes here must never fail the operation they describe. An audit insert that
throws would turn a working plate search into an error, which is a worse outcome
than a gap in the log; failures are logged loudly instead.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from services.common.db import cursor

log = logging.getLogger(__name__)

__all__ = ["record", "Action"]


class Action:
    """Canonical action strings. Constants, so a typo cannot invent a category."""

    STREAM_VIEW = "stream.view"
    PLATE_SEARCH = "plate.search"
    JOURNEY_QUERY = "journey.query"
    REPORT_EXPORT = "report.export"
    CAMERA_CREATE = "camera.create"
    CAMERA_UPDATE = "camera.update"
    CAMERA_DECOMMISSION = "camera.decommission"
    CAMERA_RECOMMISSION = "camera.recommission"
    CAMERA_IMPORT = "camera.import"
    WATCHLIST_CHANGE = "watchlist.change"
    CAMERA_OCR_BOOST = "camera.ocr_boost"
    # Failures as well as successes: a run of rejected logins against one
    # account is exactly what an audit trail should be able to show.
    AUTH_LOGIN = "auth.login"


def record(
    action: str,
    actor: str,
    subject: str | None = None,
    case_ref: str | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    """Append one audit entry. Never raises."""
    try:
        with cursor() as cur:
            cur.execute(
                "INSERT INTO audit_log (actor, action, subject, case_ref, detail)"
                " VALUES (%s, %s, %s, %s, %s)",
                (actor, action, subject, case_ref, json.dumps(detail) if detail else None),
            )
    except Exception:  # noqa: BLE001 - see module docstring
        log.exception("audit write failed: action=%s actor=%s subject=%s", action, actor, subject)
