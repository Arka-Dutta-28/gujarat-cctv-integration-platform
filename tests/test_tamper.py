"""Camera tamper detection.

The failure this guards against is specific: a camera that has been covered,
defocused or turned keeps delivering a healthy stream, so every existing check,
meaning reachability, decode rate and frame count, passes while the camera sees
nothing useful. On an estate of 80,000 nobody walks past to notice.

The tests are mostly about not crying wolf, because that is how a detector like
this fails in practice. A lorry filling the frame, a dark night, a passing
headlight: each looks momentarily like tampering and none of it is.
"""

from __future__ import annotations

import pytest

from services.anpr.tamper import (
    COVERED,
    DEFOCUSED,
    MOVED,
    MOVED_PERSIST_S,
    STABILITY_SAMPLES,
    TamperDetector,
)

np = pytest.importorskip("numpy")
cv2 = pytest.importorskip("cv2")

SIZE = (240, 320)


def street(seed: int = 0, brightness: int = 120) -> object:
    """A working camera's view: large regions of contrast, plus fine texture.

    The structure matters. An earlier version of this fixture was per-pixel
    noise, which blurs to a flat grey — so the "defocused" case tripped the
    *covered* check instead, and the test was measuring the fixture rather than
    the detector. Real footage has sky, buildings and road at different
    brightnesses, and blurring it destroys edges while leaving that contrast.
    """
    rng = np.random.default_rng(seed)
    image = np.zeros((*SIZE, 3), dtype=np.uint8)
    image[:80] = 200                # sky
    image[80:150] = brightness      # buildings
    image[150:] = 60                # road
    image[:, 140:180] = 255 - brightness   # a vertical feature
    return np.clip(
        image.astype(np.int16) + rng.integers(-25, 25, (*SIZE, 3)), 0, 255
    ).astype(np.uint8)


def covered() -> object:
    """A bag over the lens: near-uniform, with only sensor noise."""
    rng = np.random.default_rng(7)
    return np.clip(
        np.full((*SIZE, 3), 30, dtype=np.int16) + rng.integers(-2, 2, (*SIZE, 3)),
        0, 255,
    ).astype(np.uint8)


def defocused() -> object:
    """Contrast preserved, edges gone — measured: std 54, Laplacian 0.9."""
    return cv2.GaussianBlur(np.array(street(3)), (31, 31), 0)


def different_view(seed: int = 11) -> object:
    """A structurally different scene at comparable brightness.

    Deliberately not "the same view, darker": that is a lighting change, and
    telling the two apart is the entire point of the structural comparison.
    """
    rng = np.random.default_rng(seed)
    image = np.zeros((*SIZE, 3), dtype=np.uint8)
    image[:, :100] = 40       # a wall filling the left third
    image[:, 100:] = 190      # open ground
    image[40:70, :] = 90      # a horizontal feature in a different place
    return np.clip(
        image.astype(np.int16) + rng.integers(-25, 25, (*SIZE, 3)), 0, 255
    ).astype(np.uint8)


def shifting_view(step: int) -> object:
    """A scene whose *layout* is different every sample.

    This is what a generated test clip looks like to a structural signature:
    flat background with sprites moving across it, so nothing holds still. A
    camera like this has no settled view to move away from.
    """
    rng = np.random.default_rng(step)
    image = np.full((*SIZE, 3), 120, dtype=np.uint8)
    left = (step * 37) % (SIZE[1] - 60)
    top = (step * 53) % (SIZE[0] - 60)
    image[top : top + 60, left : left + 60] = 20
    return np.clip(
        image.astype(np.int16) + rng.integers(-25, 25, (*SIZE, 3)), 0, 255
    ).astype(np.uint8)


def dimmed(frame: object, factor: float = 0.2) -> object:
    """The same scene with the lights going out — dusk, or a failing floodlight."""
    return np.clip(np.array(frame) * factor, 0, 255).astype(np.uint8)


def lorry() -> object:
    """A vehicle side filling the frame: one flat colour, but textured."""
    rng = np.random.default_rng(5)
    return np.clip(
        np.full((*SIZE, 3), 150, dtype=np.int16) + rng.integers(-30, 30, (*SIZE, 3)),
        0, 255,
    ).astype(np.uint8)


def feed(detector: TamperDetector, frame: object, times: list[float]) -> list:
    return [v for t in times if (v := detector.observe(frame, t)) is not None]


class TestDetection:
    def test_a_covered_lens_is_reported(self) -> None:
        detector = TamperDetector()
        verdicts = feed(detector, covered(), [0, 5, 10, 15])
        assert verdicts and verdicts[0].kind == COVERED
        assert "covered" in verdicts[0].detail

    def test_a_defocused_lens_is_reported(self) -> None:
        detector = TamperDetector()
        verdicts = feed(detector, defocused(), [0, 5, 10, 15])
        assert verdicts and verdicts[0].kind == DEFOCUSED

    def test_a_working_camera_is_not_reported(self) -> None:
        detector = TamperDetector()
        assert feed(detector, street(), [0, 5, 10, 15, 20, 25]) == []

    def test_the_verdict_carries_the_measurement(self) -> None:
        """A threshold an operator cannot inspect is one they cannot dispute."""
        detector = TamperDetector()
        verdicts = feed(detector, covered(), [0, 5, 10, 15])
        assert verdicts[0].value >= 0
        assert str(int(verdicts[0].value)) in verdicts[0].detail


class TestNotCryingWolf:
    def test_one_bad_frame_is_not_tampering(self) -> None:
        """A headlight, a raindrop, an I-frame artefact."""
        detector = TamperDetector()
        assert detector.observe(covered(), 0.0) is None

    def test_a_lorry_filling_the_frame_is_not_a_moved_camera(self) -> None:
        """The case that would otherwise fire on every passing vehicle.

        The scene changes completely for a few seconds and then comes back. Only
        persistence separates that from a camera that has been turned.
        """
        detector = TamperDetector()
        scene = street(1)
        feed(detector, scene, [0, 5, 10])          # establish the view
        assert feed(detector, lorry(), [15, 20, 25]) == [], "still just traffic"
        assert feed(detector, scene, [30, 35]) == [], "and it has gone"

    def test_a_view_that_stays_changed_is_reported(self) -> None:
        """After the camera has demonstrated it had a settled view to change."""
        detector = TamperDetector()
        scene = street(1)
        # Long enough to establish stability — the detector will not claim a
        # camera moved until it has seen that camera hold a view.
        settle = [5.0 * i for i in range(STABILITY_SAMPLES + 2)]
        assert feed(detector, scene, settle) == []

        start = settle[-1] + 5
        times = [start + 5 * i for i in range(int(MOVED_PERSIST_S / 5) + 3)]
        verdicts = feed(detector, different_view(), times)
        assert verdicts and verdicts[0].kind == MOVED

    def test_a_condition_is_reported_once_not_every_sample(self) -> None:
        """An alert repeating every five seconds is one an operator silences."""
        detector = TamperDetector()
        verdicts = feed(detector, covered(), [0, 5, 10, 15, 20, 25, 30, 35])
        assert len(verdicts) == 1

    def test_a_recovered_camera_can_be_reported_again(self) -> None:
        """Cleared when it recovers, so a second tamper is not swallowed."""
        detector = TamperDetector()
        assert len(feed(detector, covered(), [0, 5, 10, 15])) == 1
        feed(detector, street(), [20, 25, 30])
        assert len(feed(detector, covered(), [35, 40, 45, 50])) == 1

    def test_slow_legitimate_change_never_accumulates(self) -> None:
        """Dusk, a season, a repainted wall: the reference drifts with it."""
        detector = TamperDetector()
        for i in range(40):
            frame = street(1, brightness=120 - i)
            assert detector.observe(frame, i * 5.0) is None

    def test_a_camera_with_no_settled_view_is_never_called_moved(self) -> None:
        """Measured: 39 of 50 simulated cameras reported "moved" within an hour.

        Those clips are near-flat backgrounds with vehicle sprites composited
        over them, so the signature tracks the traffic rather than the view —
        the camera has no fixed scene to move away from. None of the 31 real
        cameras, which have genuine static structure, reported anything. A
        detector cannot support a claim that a view changed when the camera has
        never demonstrated that it holds one.
        """
        detector = TamperDetector()
        # Every sample a different scene, as a clip of sprites on flat colour
        # looks to a structural signature.
        times = [5.0 * i for i in range(int(MOVED_PERSIST_S / 5) + STABILITY_SAMPLES + 10)]
        verdicts = [
            v for i, t in enumerate(times)
            if (v := detector.observe(shifting_view(i), t)) is not None
        ]
        assert [v for v in verdicts if v.kind == MOVED] == []
        assert not detector.has_stable_scene

    def test_the_lights_going_out_is_not_a_moved_camera(self) -> None:
        """The false positive this detector actually produced in service.

        Measured on cam-43: a night clip moved from headlight glare into
        darkness, mean luma 116 → 22, and the brightness-histogram comparison
        called it a moved camera with correlation 0.11. Nothing had moved. The
        comparison is now made on a contrast-normalised thumbnail, so the scene
        getting darker does not read as the scene becoming a different one.
        """
        detector = TamperDetector()
        scene = street(1)
        settle = [5.0 * i for i in range(STABILITY_SAMPLES + 2)]
        feed(detector, scene, settle)
        start = settle[-1] + 5
        times = [start + 5 * i for i in range(int(MOVED_PERSIST_S / 5) + 6)]
        assert feed(detector, dimmed(scene), times) == []

    def test_a_featureless_object_held_in_front_is_reported_as_covered(self) -> None:
        """A known limitation, pinned so it is a decision rather than a surprise.

        A smooth white panel — a stopped lorry's side, a hoarding — held across
        the lens for the confirmation window is indistinguishable from a covered
        camera by any measurement taken here, and is reported as one. That is
        arguably the right answer, since the camera genuinely sees nothing
        useful either way, but an operator should know it can happen in traffic
        rather than only through interference.
        """
        detector = TamperDetector()
        panel = np.full((*SIZE, 3), 210, dtype=np.uint8)
        verdicts = feed(detector, panel, [0, 5, 10, 15])
        assert verdicts and verdicts[0].kind == COVERED


class TestSampling:
    def test_frames_between_samples_are_ignored(self) -> None:
        """Tamper is not a per-frame question; this runs behind live decode."""
        detector = TamperDetector(sample_interval_s=5.0)
        detector.observe(street(), 0.0)
        assert detector.observe(covered(), 1.0) is None
        assert detector.last_sampled_at == 0.0

    def test_a_malformed_frame_is_not_a_tamper(self) -> None:
        detector = TamperDetector()
        assert detector.observe(None, 10.0) is None
        assert detector.observe(np.zeros((0, 0, 3), dtype=np.uint8), 20.0) is None
