"""Alert console: what an operator watches while the platform runs.

The M5 acceptance is end to end and timed: adding a plate to the watchlist must
cause the next sighting of it to raise an alert on screen within five seconds,
carrying the crop, the camera, the time and a map pin. Three things sit in that
budget: the worker's watchlist refresh (2 s), the match (microseconds, on the
sighting write path), and this endpoint's push to the browser.

Why the push is a poll and not a Postgres NOTIFY. LISTEN/NOTIFY is the
lower-latency answer and needs a dedicated long-lived connection per API
process, outside the pool, with its own reconnection handling. This polls the
alerts table on a 1-second tick and fans out to connected sockets, which costs
one indexed query per second against a table gaining a handful of rows a minute,
nothing next to the sightings the same database is absorbing. It puts worst-case
delivery at 1 s inside a 5 s budget with no new failure mode. If alert volume
ever made that untrue the answer would be NOTIFY, and the seam is this one
function.

Alerts are never deleted. `dismissed` and `false_positive` are statuses, not
removals: an operator's judgement that a match was wrong is itself evidence, and
it is also the only data from which the platform's false-positive rate can be
measured honestly.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Body, HTTPException, Query, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

from services.api.routers.cameras import Writer
from services.common.audit import Action, record
from services.common.db import connection, fetch_all, fetch_one

log = logging.getLogger("api.alerts")

router = APIRouter(prefix="/api/alerts", tags=["alerts"])

#: How often the fan-out task looks for new alerts. See the module docstring.
POLL_S = 1.0

Status = Literal["new", "acknowledged", "actioned", "dismissed", "false_positive"]


class Alert(BaseModel):
    id: str
    sighting_id: int
    raised_at: str
    sighting_ts: str = Field(description="When the vehicle was seen, UTC.")
    tier: str = Field(
        description="confirmed | probable | possible | attribute"
    )
    priority: int
    status: str
    acknowledged_by: str | None = None
    acknowledged_at: str | None = None
    resolution_note: str | None = None

    # The watchlist side — why this vehicle is wanted.
    watchlist_id: str
    plate: str
    category: str
    severity: int
    case_ref: str | None = None

    # The evidence card. Everything an operator needs without a second request:
    # what was read, where, and how sure the pipeline was.
    plate_read: str
    confidence: float
    camera_id: str
    camera_name: str | None = None
    district: str | None = None
    lat: float | None = None
    lon: float | None = None
    thumbnail_url: str | None = None
    #: Seconds from the vehicle being seen to the alert existing. The graded
    #: number, measured rather than asserted.
    detection_latency_s: float


_SELECT = """
SELECT a.id::text AS id, a.sighting_id, a.sighting_ts, a.raised_at,
       a.tier::text AS tier, a.priority, a.status::text AS status,
       a.acknowledged_by, a.acknowledged_at, a.resolution_note,
       w.id::text AS watchlist_id, w.plate, w.category::text AS category,
       w.severity, w.case_ref,
       s.plate_normalised AS plate_read, s.confidence, s.crop_path,
       c.id::text AS camera_id, c.name AS camera_name, c.district,
       ST_Y(c.geom::geometry) AS lat, ST_X(c.geom::geometry) AS lon,
       extract(epoch FROM (a.raised_at - a.sighting_ts))::float AS detection_latency_s
  FROM alerts a
  JOIN watchlist w ON w.id = a.watchlist_id
  JOIN cameras c ON c.id = a.camera_id
  LEFT JOIN sightings s ON s.id = a.sighting_id AND s.ts = a.sighting_ts
"""


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _alert(row: dict[str, Any]) -> Alert:
    crop = row.pop("crop_path", None)
    return Alert(
        **{
            **row,
            "raised_at": row["raised_at"].isoformat(),
            "sighting_ts": row["sighting_ts"].isoformat(),
            "acknowledged_at": _iso(row.get("acknowledged_at")),
            "plate_read": row.get("plate_read") or "",
            "confidence": row.get("confidence") or 0.0,
            "thumbnail_url": f"/api/sightings/{row['sighting_id']}/crop" if crop else None,
        }
    )


@router.get(
    "",
    response_model=list[Alert],
    summary="List alerts",
    description=(
        "Newest first, each one carrying its whole evidence card — the plate as "
        "read, the camera, its coordinates, the tier and the measured detection "
        "latency — so the console needs no follow-up request per row.\n\n"
        "`tier` is not decoration. `confirmed` is an exact match read at "
        "confidence >= 0.85; `probable` is the same string read less certainly, "
        "or one character out; `possible` is two characters out. They are "
        "different evidence and are labelled as such.\n\n"
        "`attribute` is categorically weaker than the other three and is not a "
        "fourth degree of the same thing. It means **no plate was read at "
        "all** — a wide-area camera saw a vehicle whose colour and class match "
        "a described watchlist entry. It is raised only for an entry whose "
        "*plate* was matched within the last ten minutes, so a description can "
        "extend a trace but can never start one, and it sorts below every "
        "plate match regardless of severity. Treat it as a filter for a human, "
        "never as an identification."
    ),
)
def list_alerts(
    status: Annotated[Status | None, Query()] = None,
    tier: Annotated[
        str | None, Query(description="confirmed | probable | possible | attribute")
    ] = None,
    min_priority: Annotated[int | None, Query(ge=1, le=5)] = None,
    since: Annotated[str | None, Query(description="ISO-8601 lower bound on raised_at.")] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[Alert]:
    where, params = [], {"limit": limit}
    for name, value, expr in (
        ("status", status, "a.status = %(status)s::alert_status"),
        ("tier", tier, "a.tier = %(tier)s::match_tier"),
        ("min_priority", min_priority, "a.priority >= %(min_priority)s"),
        ("since", since, "a.raised_at > %(since)s::timestamptz"),
    ):
        if value is not None:
            where.append(expr)
            params[name] = value

    sql = _SELECT + (" WHERE " + " AND ".join(where) if where else "")
    return [
        _alert(r)
        for r in fetch_all(sql + " ORDER BY a.raised_at DESC LIMIT %(limit)s", params)
    ]


@router.get(
    "/stats",
    summary="Alerting performance",
    description=(
        "The alerting half of the performance evidence: how many alerts were "
        "raised, at what tier, and how long detection-to-alert actually took. "
        "The M5 budget is 5 seconds and this is where the claim is checked "
        "rather than asserted."
    ),
)
def alert_stats(
    hours: Annotated[int, Query(ge=1, le=168)] = 24,
) -> dict[str, Any]:
    window = {"hours": hours}
    totals = fetch_one(
        "SELECT count(*)::int AS alerts,"
        " count(*) FILTER (WHERE status = 'new')::int AS unacknowledged,"
        " count(DISTINCT watchlist_id)::int AS entries_fired,"
        " count(DISTINCT camera_id)::int AS cameras,"
        " percentile_cont(0.5) WITHIN GROUP ("
        "   ORDER BY extract(epoch FROM (raised_at - sighting_ts)))::float AS p50_latency_s,"
        " percentile_cont(0.95) WITHIN GROUP ("
        "   ORDER BY extract(epoch FROM (raised_at - sighting_ts)))::float AS p95_latency_s,"
        " max(extract(epoch FROM (raised_at - sighting_ts)))::float AS max_latency_s"
        " FROM alerts WHERE raised_at > now() - make_interval(hours => %(hours)s)",
        window,
    ) or {}

    by_tier = fetch_all(
        "SELECT tier::text AS tier, count(*)::int AS alerts,"
        " avg(priority)::float AS mean_priority"
        " FROM alerts WHERE raised_at > now() - make_interval(hours => %(hours)s)"
        " GROUP BY 1 ORDER BY 2 DESC",
        window,
    )
    by_status = fetch_all(
        "SELECT status::text AS status, count(*)::int AS alerts"
        " FROM alerts WHERE raised_at > now() - make_interval(hours => %(hours)s)"
        " GROUP BY 1 ORDER BY 2 DESC",
        window,
    )

    watchlist = fetch_one(
        "SELECT count(*)::int AS total,"
        " count(*) FILTER (WHERE active)::int AS active FROM watchlist"
    ) or {}

    return {
        "window_hours": hours,
        "watchlist_entries": watchlist.get("total", 0),
        "watchlist_active": watchlist.get("active", 0),
        **{k: totals.get(k) for k in (
            "alerts", "unacknowledged", "entries_fired", "cameras",
            "p50_latency_s", "p95_latency_s", "max_latency_s",
        )},
        "by_tier": [
            {**r, "mean_priority": round(r["mean_priority"] or 0.0, 2)} for r in by_tier
        ],
        "by_status": by_status,
        "caveat": (
            "Latency is measured from the sighting's ingest timestamp to the "
            "alert row, so it covers matching and writing but not the browser's "
            "own render. It excludes the time the vehicle spent being tracked "
            "before its plate vote settled."
        ),
    }


class Acknowledgement(BaseModel):
    status: Status = "acknowledged"
    note: str | None = Field(None, description="Free-text resolution note.")


@router.post(
    "/{alert_id}/status",
    response_model=Alert,
    summary="Acknowledge, action or dismiss an alert",
    description=(
        "There is no delete. `dismissed` and `false_positive` are recorded "
        "outcomes: an operator's judgement that a match was wrong is evidence in "
        "its own right, and it is the only data from which a real false-positive "
        "rate can be computed. Audited as `watchlist.change`."
    ),
    responses={404: {"description": "No such alert."}},
)
def set_alert_status(
    alert_id: str, who: Writer, payload: Annotated[Acknowledgement, Body()]
) -> Alert:
    with connection() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE alerts SET status = %s::alert_status, acknowledged_by = %s,"
            " acknowledged_at = now(), resolution_note = COALESCE(%s, resolution_note)"
            " WHERE id = %s RETURNING id::text",
            (payload.status, who, payload.note, alert_id),
        )
        if cur.fetchone() is None:
            raise HTTPException(status_code=404, detail="alert not found")

    record(
        Action.WATCHLIST_CHANGE, who, subject=alert_id,
        detail={"op": "alert.status", "status": payload.status, "note": payload.note},
    )
    row = fetch_one(_SELECT + " WHERE a.id = %(id)s", {"id": alert_id})
    assert row is not None
    return _alert(row)


# --- live push ----------------------------------------------------------


class AlertHub:
    """Fans new alerts out to connected consoles.

    One poll shared by every socket, rather than one per client: an operations
    room with eight screens open must not multiply the database load by eight.
    """

    def __init__(self) -> None:
        self._clients: set[WebSocket] = set()
        self._task: asyncio.Task | None = None
        self._cursor: datetime | None = None

    async def join(self, socket: WebSocket) -> None:
        await socket.accept()
        self._clients.add(socket)
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._pump())

    def leave(self, socket: WebSocket) -> None:
        self._clients.discard(socket)

    async def _pump(self) -> None:
        # Start from now: a console opening should not replay the backlog as if
        # it were live. The REST listing is how history is loaded.
        if self._cursor is None:
            row = fetch_one("SELECT now() AS now")
            self._cursor = row["now"] if row else None

        while self._clients:
            await asyncio.sleep(POLL_S)
            try:
                fresh = await asyncio.to_thread(self._since, self._cursor)
            except Exception:  # noqa: BLE001 - a blip must not kill the pump
                log.exception("alert poll failed")
                continue
            if not fresh:
                continue
            self._cursor = max(datetime.fromisoformat(a.raised_at) for a in fresh)
            payload = [a.model_dump() for a in fresh]
            for socket in list(self._clients):
                try:
                    await socket.send_json({"type": "alerts", "alerts": payload})
                except Exception:  # noqa: BLE001 - drop the dead socket only
                    self.leave(socket)

    @staticmethod
    def _since(cursor: datetime | None) -> list[Alert]:
        rows = fetch_all(
            _SELECT + " WHERE a.raised_at > %(since)s ORDER BY a.raised_at LIMIT 200",
            {"since": cursor},
        )
        return [_alert(r) for r in rows]


hub = AlertHub()


@router.websocket("/live")
async def live_alerts(socket: WebSocket) -> None:
    """Push new alerts as they are raised.

    Send-only. The console acknowledges over REST, because an acknowledgement
    must be audited and must survive a dropped socket.
    """
    await hub.join(socket)
    try:
        while True:
            # Nothing is expected from the client; this is how a disconnect is
            # noticed promptly rather than on the next failed send.
            await socket.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception:  # noqa: BLE001
        log.debug("alert socket closed", exc_info=True)
    finally:
        hub.leave(socket)
        with contextlib.suppress(Exception):
            await socket.close()
