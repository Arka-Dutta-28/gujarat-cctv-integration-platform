"""Plate-region geometry for the training-corpus harvest.

Three coordinate spaces have to be reconciled and none of them announce
themselves: bbox and plate_bbox are in frame coordinates, the stored JPEG is the
vehicle crop so its origin is the vehicle box's top left, and that JPEG may have
been downscaled on write. Get it wrong and the crops look entirely plausible
while being cut in the wrong place, which is the worst kind of training-data
bug, because nothing raises and the model just learns less.
"""

from __future__ import annotations

import json

from scripts.harvest_plate_crops import (
    DRAWN_BOX_INSET_PX,
    MIN_PLATE_PX,
    carry_verifications,
    plate_region,
)


class TestPlateRegion:
    def test_unscaled_crop_maps_by_subtracting_the_vehicle_origin(self) -> None:
        """The common case: the vehicle box was small enough to store as-is."""
        # Vehicle at (100,200)-(500,400) → a 400x200 stored image.
        # Plate at (250,300)-(350,330) in frame coords → (150,100)-(250,130) local.
        region = plate_region([100, 200, 500, 400], [250, 300, 350, 330], 400, 200)
        assert region is not None
        x1, y1, x2, y2 = region
        assert (x1, y1) == (150 + DRAWN_BOX_INSET_PX, 100 + DRAWN_BOX_INSET_PX)
        assert (x2, y2) == (250 - DRAWN_BOX_INSET_PX, 130 - DRAWN_BOX_INSET_PX)

    def test_a_downscaled_crop_scales_both_axes_by_the_same_factor(self) -> None:
        """The crop module scales by longest edge, so one factor covers both."""
        # Vehicle 800x400 stored at 400x200 → scale 0.5.
        region = plate_region([0, 0, 800, 400], [200, 100, 400, 160], 400, 200)
        assert region is not None
        x1, y1, x2, y2 = region
        assert (x1, y1) == (100 + DRAWN_BOX_INSET_PX, 50 + DRAWN_BOX_INSET_PX)
        assert (x2, y2) == (200 - DRAWN_BOX_INSET_PX, 80 - DRAWN_BOX_INSET_PX)

    def test_the_drawn_rectangle_is_always_trimmed(self) -> None:
        """The stored crop has a 2 px box drawn round the plate, on purpose — a
        reviewer must see the pipeline boxed a plate and not a bumper sticker.
        Those pixels are not plate, and leaving them in would teach the model
        that plates have a yellow border."""
        region = plate_region([0, 0, 200, 100], [50, 40, 150, 70], 200, 100)
        assert region is not None
        x1, y1, x2, y2 = region
        assert x1 > 50 and y1 > 40 and x2 < 150 and y2 < 70

    def test_a_plate_narrower_than_the_floor_is_refused(self) -> None:
        narrow = plate_region([0, 0, 200, 100], [50, 40, 50 + MIN_PLATE_PX - 1, 70], 200, 100)
        assert narrow is None

    def test_a_degenerate_vehicle_box_is_refused_rather_than_dividing_by_zero(self) -> None:
        assert plate_region([100, 100, 100, 200], [100, 100, 150, 150], 0, 100) is None

    def test_a_plate_box_on_the_edge_is_clamped_into_the_image(self) -> None:
        """Plate boxes can extend a pixel or two past the vehicle box after
        rounding; the crop must stay inside the array or numpy silently returns
        a smaller slice."""
        region = plate_region([0, 0, 200, 100], [-5, -5, 120, 60], 200, 100)
        assert region is not None
        x1, y1, x2, y2 = region
        assert x1 >= 0 and y1 >= 0 and x2 <= 200 and y2 <= 100

    def test_the_inset_never_inverts_a_thin_box(self) -> None:
        """A plate box thinner than twice the inset must be refused, not
        returned with x2 < x1 — which numpy would accept and return empty."""
        assert plate_region([0, 0, 200, 100], [50, 40, 54, 44], 200, 100) is None


class TestCorpusDiscipline:
    def test_nothing_is_marked_verified_by_the_harvest(self) -> None:
        """The harvest emits pipeline *reads*, not ground truth.

        A pseudo-label the model produced and then trains on is a confirmation
        loop: it learns to reproduce its own errors with more confidence. Only a
        human sets `verified`, so the default must be false and must be visible
        in the manifest rather than implied by its absence.
        """
        import inspect

        from scripts import harvest_plate_crops

        source = inspect.getsource(harvest_plate_crops.main)
        assert '"verified": False' in source

    def test_a_retention_period_is_declared(self) -> None:
        """A training corpus of real number plates is not evidence and has no
        business outliving the training run."""
        from scripts.harvest_plate_crops import RETENTION_DAYS

        assert 0 < RETENTION_DAYS <= 180


class TestReviewOrdering:
    """Review sheets are ordered by how plate-like a read is, not by crop size.

    The first version sorted by width, and that is exactly backwards: a burnt-in
    banner or a shop sign is physically far wider in the frame than a number
    plate seen at distance. Sheet 0 came out almost entirely furniture —
    `CSITMS`, `SHOWROOM`, `ADVERTISE HERE`, and the camera's own clock — and a
    reviewer opening it reasonably concluded the harvest was broken. It was not.
    The data was fine and the presentation was wrong, which is a failure mode
    worth a test because nothing about it looks like a bug from the code.
    """

    def test_a_valid_plate_outranks_everything(self) -> None:
        from scripts.review_crops import plate_likeness

        assert plate_likeness("GJ08CS5454", True) == 4

    def test_digits_and_letters_rank_above_digits_alone(self) -> None:
        """Where most real plates land once OCR has mangled them a little."""
        from scripts.review_crops import plate_likeness

        assert plate_likeness("CJOIBN5396", False) > plate_likeness("13062026", False)

    def test_a_word_with_no_digits_ranks_last(self) -> None:
        """Every plate carries a four-digit number, so a read with no digits at
        all cannot be one. These are camera labels and shop hoardings."""
        from scripts.review_crops import plate_likeness

        for furniture in ("CSITMS", "SHOWROOM", "PANCHAYAT", "DELIGHT"):
            assert plate_likeness(furniture, False) == 0

    def test_a_timestamp_ranks_below_a_mangled_plate(self) -> None:
        """`13-06-2026` and `01:53:14` arrive as digit runs once the separators
        are stripped. They must not lead the sheet."""
        from scripts.review_crops import plate_likeness

        assert plate_likeness("1301256", False) < plate_likeness("GJ11CD7372", False)

    def test_ranking_never_discards(self) -> None:
        """A hard filter was measured and rejected: `>=4 digits AND >=1 letter`
        is 94% precise and finds only 16 of 51 real plates, because OCR mangles
        the state letters into digits. `5199890737` is a plate. Ranking shows it
        late; filtering would have binned it."""
        from scripts.review_crops import plate_likeness

        assert plate_likeness("5199890737", False) >= 2


class TestSheetsDoNotStraddleBands:
    """A sheet is the unit somebody opens, so it is where the boundary belongs.

    Sheets used to be cut every 40 crops regardless of band. The plate band held
    29, so the rest of sheet 0 filled with the camera's clock — and opening the
    first sheet to find timestamps in it reads as a broken harvest even when the
    ordering is correct. Reported twice from the actual files, both times right.
    """

    def test_a_clock_is_demoted_within_the_digits_only_band(self) -> None:
        """A date renders as ten digits — `13-06-2026` arrives as `1380622026`
        — which is exactly plate length, so length ordering alone sorted it to
        the very top of the band."""
        from scripts.review_crops import _within_band

        clock = _within_band({"read": "1380622026", "plate_px": 287})
        plate = _within_band({"read": "5199890737", "plate_px": 104})
        assert plate < clock, "a real plate must outrank the burnt-in date"

    def test_a_plate_with_a_year_shaped_tail_is_not_demoted(self) -> None:
        """`GJ01AB2026` is a perfectly ordinary plate. The clock rule may only
        ever look at reads with no letters in them at all."""
        from scripts.review_crops import _looks_like_a_clock

        assert _looks_like_a_clock("GJ01AB2026") is False

    def test_the_year_is_read_from_the_clock_not_written_down(self) -> None:
        """A constant `2026` would quietly stop working in January and nobody
        would notice until the sheets looked wrong again."""
        import inspect

        from scripts import review_crops

        source = inspect.getsource(review_crops._looks_like_a_clock)
        assert "datetime.now().year" in source
        assert '"2026"' not in source

    def test_plate_length_beats_width_inside_a_band(self) -> None:
        """Width is what got this wrong in the first place: the burnt-in banner
        is always the widest thing on the sheet."""
        from scripts.review_crops import _within_band

        narrow_plate = _within_band({"read": "GJ08CS5454", "plate_px": 60})
        wide_fragment = _within_band({"read": "3707", "plate_px": 278})
        assert narrow_plate < wide_fragment


class TestSheetsSayWhatTheTextIs:
    """The string under a crop is the machine's guess, not a caption.

    Reported twice as "the text doesn't match the photo" — which was true, and
    was the point: the label is what the recogniser read, and on this estate it
    is usually wrong. Nothing on the page said so, so the sheets read as though
    they were asserting what each plate says. A review tool whose output is
    mistaken for an answer is a review tool nobody can use.
    """

    def test_the_banner_names_the_text_as_the_software_s_reading(self) -> None:
        import inspect

        from scripts import review_crops

        source = inspect.getsource(review_crops._banner)
        assert "WHAT THE SOFTWARE READ" in source
        assert "WRONG" in source

    def test_each_label_is_prefixed_rather_than_bare(self) -> None:
        """A bare string under a photo reads as a caption."""
        import inspect

        from scripts import review_crops

        assert "software read:" in inspect.getsource(review_crops.build_sheets)

    def test_the_banner_fits_the_sheet_width(self) -> None:
        """A banner running off the edge is worse than none — the reader sees
        half a sentence. Sheets are only as wide as their widest crop, which can
        be under 300 px."""
        from scripts.review_crops import _banner

        for width in (200, 300, 464, 900):
            strip = _banner(4, width)
            assert strip.shape[1] == width
            assert strip.shape[0] > 0


class TestUnreviewedIsTheDefault:
    """An unreviewed row must never come back marked as ground truth.

    The `truth` column used to be pre-filled with the pipeline's read, so a reviewer
    could edit only what was wrong. --apply then certified every row in the file,
    including the hundreds nobody had looked at. 25 real reviews produced a corpus
    claiming 139 verified plates, with SHOWROOM and VIDHYA among them, and nothing
    said so.

    That is the confirmation loop this entire review step exists to prevent, and the
    tool built to prevent it was causing it.
    """

    def test_the_csv_leaves_truth_empty(self) -> None:
        import inspect

        from scripts import review_crops

        source = inspect.getsource(review_crops.build_sheets)
        assert 'writer.writerow([i, r["sighting_id"], r["read"], "", ""])' in source

    def test_an_empty_truth_is_skipped_not_certified(self) -> None:
        import inspect

        from scripts import review_crops

        source = inspect.getsource(review_crops.apply_verification)
        assert "continue  # not reviewed" in source

    def test_not_a_plate_is_explicit_rather_than_blank(self) -> None:
        """Blank now means "not looked at". Furniture needs its own marker, or
        the two states collapse back into one."""
        from scripts.review_crops import NOT_A_PLATE, UNREADABLE

        assert NOT_A_PLATE and UNREADABLE and NOT_A_PLATE != UNREADABLE

    def test_unreadable_is_distinct_from_not_a_plate(self) -> None:
        """"The camera cannot resolve this plate" is a statement about the
        camera and is the evidence behind the coverage map. Merging it into
        "not a plate" would overstate how much of the estate is readable."""
        import inspect

        from scripts import review_crops

        source = inspect.getsource(review_crops.apply_verification)
        assert '"unreadable"' in source and '"not_a_plate"' in source


class TestMarkersAreCaseInsensitive:
    """`x` and `X` must both mean "not a plate".

    `truth` is upper-cased on the way in, because a plate is upper case. That
    silently broke the markers: a reviewer's `x` became `X`, stopped matching
    NOT_A_PLATE, and fell through to the "this is the correct plate" branch — so
    81 pieces of signage came back verified as plates reading `X`.

    The same shape of bug as the pre-filled truth column: a convenience applied
    to one field quietly corrupting another.
    """

    def test_lowercase_x_is_recognised_as_not_a_plate(self, tmp_path) -> None:
        import csv
        import json

        from scripts.review_crops import apply_verification

        corpus = tmp_path / "corpus"
        corpus.mkdir()
        (corpus / "manifest.jsonl").write_text(
            json.dumps({"sighting_id": "s1", "read": "SHOWROOM", "verified": False}) + "\n"
            + json.dumps({"sighting_id": "s2", "read": "GJ01AB1234", "verified": False}) + "\n"
        )
        csv_path = tmp_path / "verify.csv"
        with csv_path.open("w", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["index", "sighting_id", "read", "truth", "note"])
            writer.writerow([0, "s1", "SHOWROOM", "x", "signage"])
            writer.writerow([1, "s2", "GJ01AB1234", "gj01ab1234", ""])

        assert apply_verification(corpus, csv_path) == 1  # one plate, not two
        lines = (corpus / "manifest.jsonl").read_text().splitlines()
        rows = [json.loads(line) for line in lines]
        assert rows[0]["not_a_plate"] is True
        assert rows[0]["truth"] is None
        # A plate typed in lower case is still a plate, and is upper-cased.
        assert rows[1]["truth"] == "GJ01AB1234"


class TestCarryVerifications:
    """A re-harvest rebuilds the manifest from the database.

    Without a merge it silently discards every human verdict in it — and a
    re-harvest is what happens when new footage arrives, which is exactly when
    the existing labels are worth most.
    """

    def _corpus(self, tmp_path, rows):
        (tmp_path / "manifest.jsonl").write_text(
            "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8"
        )
        return tmp_path

    def test_a_verified_row_keeps_its_truth(self, tmp_path):
        old = self._corpus(tmp_path, [
            {"sighting_id": "1", "verified": True, "truth": "GJ01AB1234",
             "not_a_plate": False, "unreadable": False},
        ])
        fresh = [{"sighting_id": "1", "verified": False}]
        assert carry_verifications(old, fresh) == 1
        assert fresh[0] == {"sighting_id": "1", "verified": True, "truth": "GJ01AB1234",
                            "not_a_plate": False, "unreadable": False}

    def test_not_a_plate_and_unreadable_both_survive(self, tmp_path):
        old = self._corpus(tmp_path, [
            {"sighting_id": "1", "verified": True, "truth": None,
             "not_a_plate": True, "unreadable": False},
            {"sighting_id": "2", "verified": True, "truth": None,
             "not_a_plate": False, "unreadable": True},
        ])
        fresh = [{"sighting_id": "1", "verified": False}, {"sighting_id": "2", "verified": False}]
        carry_verifications(old, fresh)
        assert fresh[0]["not_a_plate"] is True
        assert fresh[1]["unreadable"] is True

    def test_an_unverified_row_is_left_alone(self, tmp_path):
        old = self._corpus(tmp_path, [{"sighting_id": "1", "verified": False, "truth": None}])
        fresh = [{"sighting_id": "1", "verified": False}]
        assert carry_verifications(old, fresh) == 0

    def test_a_newly_harvested_crop_is_untouched(self, tmp_path):
        old = self._corpus(tmp_path, [{"sighting_id": "1", "verified": True, "truth": "X"}])
        fresh = [{"sighting_id": "2", "verified": False}]
        assert carry_verifications(old, fresh) == 0
        assert fresh[0]["verified"] is False

    def test_a_first_harvest_has_nothing_to_carry(self, tmp_path):
        assert carry_verifications(tmp_path, [{"sighting_id": "1"}]) == 0
