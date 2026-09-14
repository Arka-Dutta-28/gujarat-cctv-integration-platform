"""Where the planted pass sits in the corridor clip.

One number decides whether the demo's journey segment works, and it is not
obvious which one. A camera with playback offset `O` sees clip second `P` at
wall time `(P - O) mod duration`, so the *upstream* camera — the one carrying
the largest offset — sees the pass `P - max(offset)` seconds after its stream
starts. That difference is its entire margin, and if the estate has not
finished connecting by then, the upstream camera misses the pass and picks up
the following cycle instead.

Measured 7 September 2026: at a 20-second margin cam-20 was due 38 s after
stream start, missed it, and appeared 384 s later than the corridor implies.
cam-21 through cam-23 arrived at 221 s and 237 s — exactly right — so the
journey had five plausible hops and one impossible one, and the plausibility
check correctly refused to call it one vehicle.
"""

from __future__ import annotations

import random

from scripts.make_test_videos import JOURNEY_PLATE, JOURNEY_START_MARGIN_S, build_journey_clip

HOP_S = 238.0
CAMERAS = 6


def corridor_offsets(cameras: int = CAMERAS, hop: float = HOP_S) -> list[float]:
    """What `seed.py:_apply_journey_corridor` produces: decreasing, one hop apart."""
    return [(cameras - 1 - i) * hop for i in range(cameras)]


def planted_at(clip) -> float:
    return next(p.start_s for p in clip.passes if p.plate == JOURNEY_PLATE)


def _clip(**kw):
    return build_journey_clip(
        duration_s=kw.pop("duration_s", 1548.0), width=960, height=540, fps=15.0,
        rng=random.Random(0), hop_s=HOP_S, cameras=CAMERAS, **kw,
    )


class TestPlantedPosition:
    def test_the_upstream_camera_gets_a_full_hop_of_margin(self):
        # The bug: at 20 seconds flat this margin was swallowed by the time the
        # estate takes to connect fifty streams and warm its models.
        clip = _clip()
        assert planted_at(clip) - max(corridor_offsets()) == HOP_S * JOURNEY_START_MARGIN_S

    def test_the_pass_clears_the_wrap_floor(self):
        # Below (cameras - 1) hops the first camera's sighting wraps past the
        # last one's and the trace runs backwards down the corridor.
        assert planted_at(_clip()) >= (CAMERAS - 1) * HOP_S

    def test_every_camera_sees_it_in_corridor_order(self):
        """Upstream first, one hop apart, no wrap. The property that matters."""
        clip = _clip()
        p, d = planted_at(clip), clip.duration_s
        seen = [(p - o) % d for o in corridor_offsets()]
        assert seen == sorted(seen), "corridor order broken — the trace runs backwards"
        gaps = [b - a for a, b in zip(seen, seen[1:], strict=False)]
        assert all(abs(g - HOP_S) < 1.0 for g in gaps), gaps

    def test_the_clip_is_long_enough_to_hold_it(self):
        clip = _clip()
        assert planted_at(clip) < clip.duration_s

    def test_an_explicit_position_is_respected(self):
        # The default is a floor, not a policy. A caller measuring the wrap
        # behaviour deliberately must be able to place the pass anywhere.
        assert planted_at(_clip(appear_at_s=100.0)) == 100.0

    def test_the_planted_plate_is_present_exactly_once(self):
        # Two appearances of the trace vehicle in one clip would be two passes
        # of the corridor, which is an implausible transition by construction.
        clip = _clip()
        assert sum(1 for p in clip.passes if p.plate == JOURNEY_PLATE) == 1
