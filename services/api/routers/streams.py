"""Live view.

One endpoint answers "how do I watch this camera?" for every camera type. The
caller never learns whether the source was RTSP, progressive HTTP or a file —
that is the adapter spine's job — and never sees a credential.

Every call writes a `stream.view` audit row. Watching a public-space camera is
an access to personal data, so it is recorded with who did it and, where the
operator supplied one, the case reference that justifies it.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

import httpx
from fastapi import APIRouter, Header, HTTPException, Query
from pydantic import BaseModel, Field

from services.adapters import CameraRef, available_adapters, get_adapter
from services.api.routers.cameras import Actor
from services.common.audit import Action, record
from services.common.config import settings
from services.common.db import fetch_one
from services.relay.relay import UNSTABLE_RESTARTS

log = logging.getLogger("api.streams")

router = APIRouter(prefix="/api/cameras", tags=["streams"])

RELAY_TIMEOUT_S = 8.0


class StreamTarget(BaseModel):
    camera_id: str
    camera_name: str
    adapter: str
    protocol: str = Field(description="`webrtc`, `hls` or `file`.")
    url: str = Field(description="Where the player should connect. Never contains credentials.")
    ready: bool = Field(description="False means the relay is still starting; retry shortly.")
    unstable: bool = Field(
        False,
        description=(
            "The relay for this camera keeps dying and restarting, so the "
            "stream may establish and then deliver nothing. `ready` cannot "
            "express this: a flapping publisher is attached to the media server "
            "at the instant the path is polled. Reported separately so a client "
            "can say the upstream is at fault rather than retrying silently."
        ),
    )
    relayed: bool = Field(description="True when the platform is pulling this source.")
    transcoded: bool = Field(
        default=False,
        description=(
            "True when the platform re-encoded this camera for the browser. The "
            "source is left untouched; analytics still read it at full quality."
        ),
    )
    detail: str | None = None


def _camera_ref(camera_id: str) -> tuple[CameraRef, str]:
    row = fetch_one(
        "SELECT id::text AS id, external_ref, name, adapter::text AS adapter,"
        " stream_ref, credential_ref, kind::text AS kind, status::text AS status"
        " FROM cameras WHERE id = %(id)s",
        {"id": camera_id},
    )
    if row is None:
        raise HTTPException(status_code=404, detail="camera not found")
    status = row.pop("status")
    return CameraRef(**row), status


@router.get(
    "/{camera_id}/stream",
    response_model=StreamTarget,
    summary="Get a playable stream for a camera",
    description=(
        "Resolves a camera to something a browser can play, whatever its native "
        "protocol. RTSP and progressive-HTTP sources are pulled into the media "
        "server and returned as WebRTC; unauthenticated HLS is returned "
        "directly.\n\n"
        "Video is pulled **on demand** — a relay starts when this is called and "
        "stops itself once nobody is watching, so the platform does not haul "
        "video it is not using.\n\n"
        "`ready: false` means the relay is still starting; retry in a moment "
        "rather than treating it as an error.\n\n"
        "Writes a `stream.view` audit row. Pass `X-Case-Ref` to bind the access "
        "to an investigation."
    ),
    responses={
        404: {"description": "No such camera."},
        409: {"description": "Camera is decommissioned."},
        503: {"description": "The relay supervisor is unreachable."},
    },
)
def get_stream(
    camera_id: str,
    who: Actor,
    case_ref: Annotated[str | None, Header(alias="X-Case-Ref")] = None,
    wait: Annotated[bool, Query(description="Wait briefly for the relay to be ready.")] = True,
) -> StreamTarget:
    camera, status = _camera_ref(camera_id)

    if status == "decommissioned":
        raise HTTPException(status_code=409, detail="camera is decommissioned")

    try:
        adapter = get_adapter(camera.adapter)
    except LookupError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    target = adapter.playback(camera)
    ready, relayed, detail = True, target.requires_relay, target.detail
    unstable = False
    transcoded = False

    if target.requires_relay and wait:
        # The API never handles media itself; it asks the relay supervisor to
        # pull the source and reports what it says.
        try:
            resp = httpx.post(
                f"{settings.relay_url}/relay/{camera_id}", timeout=RELAY_TIMEOUT_S
            )
            resp.raise_for_status()
            state = resp.json()
            ready = bool(state.get("ready"))
            unstable = int(state.get("restarts_recent") or 0) >= UNSTABLE_RESTARTS
            transcoded = bool(state.get("transcoded"))
            detail = state.get("detail") or detail
            if state.get("playback_url"):
                target.url = state["playback_url"]
                # The relay decides the transport, because it is what built the
                # URL. Leaving the adapter's guess in place here would hand the
                # player a playlist labelled `webrtc` on a hosted instance, and
                # a WHEP handshake against an m3u8 fails without saying so.
                target.protocol = state.get("playback_protocol") or target.protocol
        except httpx.HTTPError as exc:
            log.warning("relay supervisor unreachable for %s: %s", camera_id, exc)
            raise HTTPException(
                status_code=503,
                detail=f"relay supervisor unreachable: {exc}",
            ) from exc

    record(
        Action.STREAM_VIEW, who, subject=camera_id, case_ref=case_ref,
        detail={
            "adapter": camera.adapter, "protocol": target.protocol,
            "relayed": relayed, "transcoded": transcoded,
        },
    )

    return StreamTarget(
        camera_id=camera.id,
        camera_name=camera.name,
        adapter=camera.adapter,
        protocol=target.protocol,
        url=target.url,
        ready=ready,
        unstable=unstable,
        relayed=relayed,
        transcoded=transcoded,
        detail=detail,
    )


@router.get(
    "/adapters/registered",
    summary="List registered adapters",
    description=(
        "Every camera type the platform can ingest. Populated by the adapters "
        "registering themselves — nothing enumerates this list by hand, which is "
        "what lets a new adapter be added without touching anything outside its "
        "own module."
    ),
)
def list_adapters() -> dict[str, Any]:
    adapters = available_adapters()
    counts = fetch_one(
        "SELECT jsonb_object_agg(adapter, n) AS counts FROM ("
        " SELECT adapter::text AS adapter, count(*) AS n FROM cameras"
        " WHERE status <> 'decommissioned' GROUP BY 1) x"
    )
    in_use = (counts or {}).get("counts") or {}
    return {
        "count": len(adapters),
        "adapters": [
            {**a.describe(), "cameras_registered": in_use.get(name, 0)}
            for name, a in sorted(adapters.items())
        ],
    }
