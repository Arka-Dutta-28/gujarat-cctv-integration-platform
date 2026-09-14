"""The synthetic plate generator behind the recogniser training (Stage 1).

A generator that emits an invalid label, or a crop the recogniser's input step
cannot take, silently teaches the wrong thing for an hour before anyone looks.
"""

from __future__ import annotations

import random

import numpy as np

from scripts.synth_plates import INPUT_H, INPUT_W, pair, random_plate, sample, to_input
from services.common.plates import is_valid_format


def test_every_label_is_a_valid_indian_plate() -> None:
    rng = random.Random(7)
    assert all(is_valid_format(random_plate(rng)) for _ in range(2000))


def test_rto_codes_are_not_limited_to_the_corridor() -> None:
    """The corridor clips use 8 Gujarat RTOs; real test plates include GJ35, GJ09."""
    rng = random.Random(7)
    rtos = {p[2:4] for p in (random_plate(rng) for _ in range(3000)) if p.startswith("GJ")}
    assert {"09", "35", "11"} <= rtos


def test_samples_fit_the_measured_size_and_the_model_input() -> None:
    rng = random.Random(7)
    for _ in range(50):
        img, label = sample(rng)
        assert img.dtype == np.uint8 and img.ndim == 2
        assert 25 <= img.shape[1] <= 150, "width outside the measured p5-p95 band"
        x = to_input(img)
        assert x.shape == (INPUT_H, INPUT_W)
        assert x.min() >= 0.0 and x.max() <= 1.0
        assert is_valid_format(label)


def test_super_resolution_pairs_line_up_and_leave_the_damage_unchanged() -> None:
    """The clean target is 4x the damaged crop, and asking for it draws no randomness."""
    damaged, clean, label = pair(random.Random(42))
    alone, same_label = sample(random.Random(42))
    assert clean.shape == (damaged.shape[0] * 4, damaged.shape[1] * 4)
    assert label == same_label and np.array_equal(damaged, alone)
