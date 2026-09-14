"""Camera health decisions, as pure functions.

Kept separate from the prober's I/O so the rules can be tested without a media
server or a database. The rules themselves carry two lessons from the real feeds
(docs/field-observations.md).

No traffic is not a fault. Several real cameras point at a market stall or an
empty lane and will never produce a detection. A camera publishing frames is
online whether or not anything drives past it.

Camera clocks lie. Two of the four sampled feeds were about two months out and
disagreed with each other. Skew is therefore reported as a health signal, and
never used to decide whether a stream is live.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

__all__ = [
    "CameraStatus",
    "StreamSample",
    "HealthVerdict",
    "assess",
    "STALL_SECONDS",
    "CLOCK_SKEW_WARN_S",
]

# A publisher connected but sending nothing for this long is stalled, not live.
# Generous enough to survive the 4 fps degraded profile and a keyframe gap.
STALL_SECONDS = 12.0

# Below this share of the expected bitrate the picture is arriving, but badly.
DEGRADED_BITRATE_RATIO = 0.35

# Stream time drifting from platform time by more than this is worth surfacing.
# Not a failure: it is the normal state of the real estate, and the reason
# sightings are stamped on ingest.
CLOCK_SKEW_WARN_S = 60.0


class CameraStatus(StrEnum):
    """Mirrors the `camera_status` enum in the database."""

    ONLINE = "online"
    OFFLINE = "offline"
    DEGRADED = "degraded"
    UNKNOWN = "unknown"
    DECOMMISSIONED = "decommissioned"


@dataclass(frozen=True)
class StreamSample:
    """One observation of a stream, from whichever probe produced it."""

    #: Whether a publisher is connected and the path is serving.
    ready: bool
    #: Seconds since bytes last advanced. None when never observed.
    seconds_since_data: float | None = None
    #: Observed bitrate over the sampling window, bits per second.
    bitrate_bps: float | None = None
    #: What the registry says this camera should roughly produce.
    expected_bitrate_bps: float | None = None
    #: Stream clock minus platform clock, seconds. Reported, never acted on.
    clock_skew_s: float | None = None
    #: Probe could not run at all (network error, media server down).
    probe_failed: bool = False
    #: Error detail for the operator, if any.
    error: str | None = None


@dataclass(frozen=True)
class HealthVerdict:
    status: CameraStatus
    reason: str
    #: True when the stream is up but the clock is materially wrong.
    clock_suspect: bool = False


def assess(sample: StreamSample) -> HealthVerdict:
    """Decide a camera's status from one observation.

    Deliberately conservative about calling something offline: a camera the
    operator can still see pictures from must never show red because our
    bitrate arithmetic disagreed with it.
    """
    if sample.probe_failed:
        # We failed to look, which says nothing about the camera. Claiming
        # offline here would paint the whole map red the moment the media
        # server hiccups.
        return HealthVerdict(
            CameraStatus.UNKNOWN,
            f"probe failed: {sample.error or 'unknown error'}",
        )

    if not sample.ready:
        return HealthVerdict(
            CameraStatus.OFFLINE, sample.error or "no publisher connected"
        )

    skew = sample.clock_skew_s
    clock_suspect = skew is not None and abs(skew) > CLOCK_SKEW_WARN_S

    if sample.seconds_since_data is not None and sample.seconds_since_data > STALL_SECONDS:
        return HealthVerdict(
            CameraStatus.DEGRADED,
            f"publisher connected but no data for {sample.seconds_since_data:.0f}s",
            clock_suspect,
        )

    if (
        sample.bitrate_bps is not None
        and sample.expected_bitrate_bps
        and sample.bitrate_bps < sample.expected_bitrate_bps * DEGRADED_BITRATE_RATIO
    ):
        return HealthVerdict(
            CameraStatus.DEGRADED,
            (
                f"bitrate {sample.bitrate_bps / 1000:.0f} kbit/s is far below the "
                f"expected {sample.expected_bitrate_bps / 1000:.0f} kbit/s"
            ),
            clock_suspect,
        )

    # Streaming normally. Note what is *not* checked here: whether any vehicle
    # was detected. A camera watching an empty lane is healthy.
    reason = "streaming"
    if clock_suspect:
        reason = f"streaming; camera clock off by {skew:+.0f}s"
    return HealthVerdict(CameraStatus.ONLINE, reason, clock_suspect)
