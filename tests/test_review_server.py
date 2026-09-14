"""The corpus review tool: blind labelling, re-checks, and disputed crops.

The tests that matter here are not about HTTP. They are about the three
properties that decide whether the labels this tool produces are worth anything.

1. Blind means blind. If the pipeline's read reaches the page before the
   reviewer answers, the labels are a correction of the recogniser rather than
   an independent reading of the image, and any accuracy figure measured against
   them is measuring the model's own homework. This project has already made
   that mistake once, in the confusion learner.
2. Re-checks actually happen. The agreement number is the ceiling on what a
   fine-tuning result can claim. If the cadence silently produces zero re-checks
   in a short session, the number does not exist and nobody notices.
3. Disagreement is preserved. Quietly picking a winner would delete the one
   measurement the double-labelling was for.
"""

from __future__ import annotations

import json
import pathlib
import threading
import urllib.error
import urllib.request

import pytest

from scripts.review_server import (
    EXPORT_COLUMNS,
    Corpus,
    Handler,
    agreement,
    apply_labels,
    choose_next,
    export_csv,
    label_event,
    latest_by_labeller,
    load_events,
    main,
    queue_order,
    rename_labeller,
    row_payload,
    seed_from_manifest,
    table_rows,
)


def crop(sighting_id: str, read: str = "GJ01AB1234", **kw) -> dict:
    row = {
        "image": f"images/{sighting_id}.png",
        "sighting_id": sighting_id,
        "read": read,
        "normalised": read,
        "confidence": 0.7,
        "condition": "day",
        "format_valid": kw.pop("format_valid", True),
        "camera": "sentinel-cam21",
        "camera_name": "21 Test Road",
        "ts": "2026-09-01T10:00:00+00:00",
        "plate_px": kw.pop("plate_px", 80),
    }
    row.update(kw)
    return row


@pytest.fixture
def corpus(tmp_path: pathlib.Path) -> pathlib.Path:
    rows = [crop(str(i)) for i in range(30)]
    (tmp_path / "images").mkdir()
    for row in rows:
        # A one-pixel PNG is enough; nothing here decodes it.
        (tmp_path / row["image"]).write_bytes(b"\x89PNG\r\n\x1a\n")
    (tmp_path / "manifest.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8"
    )
    return tmp_path


def event(sighting_id: str, labeller: str, verdict: str = "plate", truth: str = "GJ01AB1234",
          **kw) -> dict:
    return label_event(
        crop(sighting_id),
        verdict=verdict,
        truth=truth,
        note=kw.pop("note", ""),
        labeller=labeller,
        mode=kw.pop("mode", "blind"),
        recheck=kw.pop("recheck", False),
    )


class TestBlindMeansBlind:
    def test_the_blind_payload_carries_no_machine_read(self):
        payload = row_payload(crop("1", read="GIIEAFOSE2"), False, "blind")
        assert "machine_read" not in payload
        assert "confidence" not in payload
        # And nothing else smuggles it through under another name.
        assert "GIIEAFOSE2" not in json.dumps(payload)

    def test_triage_shows_the_read_because_that_is_what_triage_is_for(self):
        payload = row_payload(crop("1", read="GIIEAFOSE2"), False, "triage")
        assert payload["machine_read"] == "GIIEAFOSE2"

    def test_a_triage_label_is_marked_anchored(self):
        assert event("1", "a", mode="triage")["anchored"] is True
        assert event("1", "a", mode="blind")["anchored"] is False

    def test_the_machine_read_is_frozen_onto_the_event(self):
        # So an event stays interpretable after the recogniser is retrained and
        # the manifest's `read` no longer says what the human was disagreeing
        # with.
        assert event("1", "a")["machine_read"] == "GJ01AB1234"


class TestVerdicts:
    def test_a_plate_is_upper_cased(self):
        assert event("1", "a", truth="gj01ab1234")["truth"] == "GJ01AB1234"

    def test_a_non_plate_verdict_carries_no_truth(self):
        assert event("1", "a", verdict="not_a_plate", truth="ignored")["truth"] is None
        assert event("1", "a", verdict="unreadable", truth="ignored")["truth"] is None

    def test_an_unknown_verdict_is_refused(self):
        with pytest.raises(ValueError, match="unknown verdict"):
            event("1", "a", verdict="probably")

    def test_a_skip_is_not_a_label(self):
        # Skipping must leave the crop in the queue for someone else, so it can
        # never look like an answer.
        events = [event("1", "a", verdict="skip")]
        assert latest_by_labeller(events) == {}


class TestQueue:
    def test_format_valid_crops_come_first_and_wordless_ones_last(self):
        rows = [
            crop("word", read="VIDHYA", format_valid=False, plate_px=200),
            crop("plate", read="GJ01AB1234", format_valid=True, plate_px=40),
            crop("digits", read="1380622026", format_valid=False, plate_px=150),
        ]
        # Width-first would put the signage on top; that is the bug the ranking
        # in review_crops exists to fix, and this tool must not reintroduce it.
        assert [r["sighting_id"] for r in queue_order(rows)][0] == "plate"
        assert [r["sighting_id"] for r in queue_order(rows)][-1] == "word"

    def test_a_labeller_is_never_shown_their_own_crop_twice(self):
        rows = [crop(str(i)) for i in range(3)]
        events = [event("0", "reviewer-a"), event("1", "reviewer-a")]
        row, _ = choose_next(rows, events, "reviewer-a", served=1)
        assert row["sighting_id"] == "2"

    def test_the_queue_ends_when_this_labeller_has_seen_everything(self):
        rows = [crop("0")]
        row, recheck = choose_next(rows, [event("0", "reviewer-a")], "reviewer-a", served=1)
        assert row is None and recheck is False

    def test_every_tenth_crop_is_someone_elses_already_labelled_one(self):
        rows = [crop(str(i)) for i in range(30)]
        events = [event("7", "reviewer-b")]
        row, recheck = choose_next(rows, events, "reviewer-a", served=10)
        assert recheck is True
        assert row["sighting_id"] == "7"

    def test_the_cadence_is_exact_rather_than_probable(self):
        # A 10% random rate produces zero re-checks in a short session often
        # enough to matter, and then the agreement number silently does not
        # exist. Deterministic means a 20-crop session always yields two.
        rows = [crop(str(i)) for i in range(30)]
        events = [event(str(i), "reviewer-b") for i in range(5)]
        rechecks = [choose_next(rows, events, "reviewer-a", served=n)[1] for n in range(1, 21)]
        assert sum(rechecks) == 2

    def test_a_recheck_falls_back_to_new_work_when_nobody_else_has_labelled(self):
        rows = [crop(str(i)) for i in range(30)]
        row, recheck = choose_next(rows, [event("3", "reviewer-a")], "reviewer-a", served=10)
        assert recheck is False
        assert row is not None and row["sighting_id"] != "3"

    def test_my_own_labels_are_never_served_back_to_me_as_a_recheck(self):
        rows = [crop("0"), crop("1")]
        row, recheck = choose_next(rows, [event("0", "reviewer-a")], "reviewer-a", served=10)
        assert recheck is False and row["sighting_id"] == "1"


class TestApply:
    def test_agreement_is_applied(self, corpus: pathlib.Path):
        (corpus / "labels.jsonl").write_text(
            json.dumps(event("0", "reviewer-a", truth="GJ01AB1234")) + "\n"
            + json.dumps(event("0", "reviewer-b", truth="GJ01AB1234")) + "\n",
            encoding="utf-8",
        )
        stats = apply_labels(corpus)
        assert stats["plates"] == 1 and stats["disputed"] == 0
        row = next(r for r in _manifest(corpus) if r["sighting_id"] == "0")
        assert row["verified"] is True and row["truth"] == "GJ01AB1234"

    def test_a_disagreement_is_left_unverified_and_marked(self, corpus: pathlib.Path):
        (corpus / "labels.jsonl").write_text(
            json.dumps(event("0", "reviewer-a", truth="GJ01AB1234")) + "\n"
            + json.dumps(event("0", "reviewer-b", truth="GJ01AB1284")) + "\n",
            encoding="utf-8",
        )
        stats = apply_labels(corpus)
        assert stats["disputed"] == 1 and stats["plates"] == 0
        row = next(r for r in _manifest(corpus) if r["sighting_id"] == "0")
        assert row["disputed"] is True
        assert row["verified"] is False

    def test_unreadable_is_not_collapsed_into_not_a_plate(self, corpus: pathlib.Path):
        # "the camera cannot resolve this plate" is a statement about the
        # camera and is the evidence behind the coverage map. Merging it into
        # "there was no plate" would overstate how much of the estate is
        # readable.
        (corpus / "labels.jsonl").write_text(
            json.dumps(event("0", "reviewer-a", verdict="unreadable")) + "\n"
            + json.dumps(event("1", "reviewer-a", verdict="not_a_plate")) + "\n",
            encoding="utf-8",
        )
        apply_labels(corpus)
        rows = {r["sighting_id"]: r for r in _manifest(corpus)}
        assert rows["0"]["unreadable"] is True and rows["0"]["not_a_plate"] is False
        assert rows["1"]["not_a_plate"] is True and rows["1"]["unreadable"] is False

    def test_a_labellers_second_answer_replaces_their_first(self, corpus: pathlib.Path):
        (corpus / "labels.jsonl").write_text(
            json.dumps(event("0", "reviewer-a", truth="GJ01AB1111")) + "\n"
            + json.dumps(event("0", "reviewer-a", truth="GJ01AB1234")) + "\n",
            encoding="utf-8",
        )
        apply_labels(corpus)
        row = next(r for r in _manifest(corpus) if r["sighting_id"] == "0")
        # One person correcting their own typo is not a dispute.
        assert row.get("disputed") is not True
        assert row["truth"] == "GJ01AB1234"

    def test_anchored_labels_can_be_excluded(self, corpus: pathlib.Path):
        (corpus / "labels.jsonl").write_text(
            json.dumps(event("0", "reviewer-a", mode="triage")) + "\n"
            + json.dumps(event("1", "reviewer-a", mode="blind")) + "\n",
            encoding="utf-8",
        )
        stats = apply_labels(corpus, include_anchored=False)
        assert stats["plates"] == 1
        rows = {r["sighting_id"]: r for r in _manifest(corpus)}
        assert rows["1"]["verified"] is True
        assert rows["0"].get("verified") is not True

    def test_a_resolved_dispute_clears_the_flag(self, corpus: pathlib.Path):
        path = corpus / "labels.jsonl"
        path.write_text(
            json.dumps(event("0", "reviewer-a", truth="GJ01AB1234")) + "\n"
            + json.dumps(event("0", "reviewer-b", truth="GJ01AB1284")) + "\n",
            encoding="utf-8",
        )
        apply_labels(corpus)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event("0", "reviewer-b", truth="GJ01AB1234")) + "\n")
        apply_labels(corpus)
        row = next(r for r in _manifest(corpus) if r["sighting_id"] == "0")
        assert "disputed" not in row and row["verified"] is True


class TestAgreement:
    def test_category_and_character_agreement_are_reported_separately(self):
        events = [
            event("0", "a", truth="GJ01AB1234"), event("0", "b", truth="GJ01AB1234"),
            event("1", "a", truth="GJ01AB1234"), event("1", "b", truth="GJ01AB1284"),
            event("2", "a", verdict="not_a_plate"), event("2", "b", verdict="unreadable"),
            event("3", "a", truth="GJ01AB1234"),
        ]
        report = agreement(events)
        assert report["double_labelled"] == 3      # crop 3 was seen by one person
        assert report["category_agreement"] == 2   # crops 0 and 1 are both "plate"
        assert report["plate_pairs"] == 2
        assert report["exact_agreement"] == 1      # only crop 0 reads the same
        assert report["disputed"] == ["1", "2"]

    def test_no_double_labelling_reports_nothing_rather_than_a_perfect_score(self):
        # A 100% agreement rate computed over zero pairs is the most flattering
        # possible way to say "we never checked".
        report = agreement([event("0", "a")])
        assert report["double_labelled"] == 0 and report["exact_agreement"] == 0


class TestEventLog:
    def test_a_torn_final_line_does_not_stop_the_session(self, corpus: pathlib.Path):
        (corpus / "labels.jsonl").write_text(
            json.dumps(event("0", "reviewer-a")) + "\n{\"sighting_id\": \"1\", \"verd",
            encoding="utf-8",
        )
        assert len(load_events(corpus)) == 1

    def test_a_missing_log_is_an_empty_one(self, corpus: pathlib.Path):
        assert load_events(corpus) == []


class TestServer:
    """One end-to-end pass, because the wiring is where blind mode would leak."""

    @pytest.fixture
    def server(self, corpus: pathlib.Path):
        import http.server

        handler = type("Bound", (Handler,), {"corpus": Corpus(corpus)})
        httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        yield f"http://127.0.0.1:{httpd.server_address[1]}", corpus
        httpd.shutdown()
        httpd.server_close()

    def _get(self, base: str, path: str) -> dict:
        with urllib.request.urlopen(base + path, timeout=5) as response:
            return json.loads(response.read())

    def test_next_then_label_reveals_only_afterwards(self, server):
        base, _ = server
        served = self._get(base, "/api/next?labeller=reviewer-a&mode=blind")
        assert "machine_read" not in served["item"]

        body = json.dumps({
            "sighting_id": served["item"]["sighting_id"],
            "labeller": "reviewer-a", "mode": "blind", "verdict": "plate", "truth": "gj01ab1234",
        }).encode()
        request = urllib.request.Request(
            base + "/api/label", data=body, headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            revealed = json.loads(response.read())
        assert revealed["machine_read"] == "GJ01AB1234"
        assert revealed["progress"]["labelled"] == 1

    def test_the_label_is_durable_before_the_reveal_is_sent(self, server):
        base, corpus = server
        served = self._get(base, "/api/next?labeller=reviewer-a")
        body = json.dumps({
            "sighting_id": served["item"]["sighting_id"],
            "labeller": "reviewer-a", "verdict": "not_a_plate",
        }).encode()
        request = urllib.request.Request(
            base + "/api/label", data=body, headers={"Content-Type": "application/json"}
        )
        urllib.request.urlopen(request, timeout=5).read()
        # On disk, not just in memory — a click is annoying to have to give twice.
        assert len(load_events(corpus)) == 1

    def test_a_crop_path_cannot_escape_the_corpus(self, server):
        base, corpus = server
        rows = _manifest(corpus)
        rows[0]["image"] = "../../../etc/passwd"
        (corpus / "manifest.jsonl").write_text(
            "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8"
        )
        import http.server

        handler = type("Bound2", (Handler,), {"corpus": Corpus(corpus)})
        httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            url = f"http://127.0.0.1:{httpd.server_address[1]}/crop/{rows[0]['sighting_id']}"
            with pytest.raises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(url, timeout=5)
            assert caught.value.code == 404
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_a_labeller_is_required(self, server):
        base, _ = server
        with pytest.raises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(base + "/api/next", timeout=5)
        assert caught.value.code == 400


class TestBinding:
    def test_a_non_loopback_bind_is_refused(self, corpus: pathlib.Path):
        # The corpus is real registration numbers held under PURPOSE.md's
        # "not copied outside this machine".
        assert main(["--corpus", str(corpus), "--host", "0.0.0.0"]) == 2

    def test_a_missing_corpus_is_an_error_not_a_traceback(self, tmp_path: pathlib.Path):
        assert main(["--corpus", str(tmp_path)]) == 1


def _manifest(corpus: pathlib.Path) -> list[dict]:
    return [
        json.loads(line)
        for line in (corpus / "manifest.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


class TestSeeding:
    """The 141 crops verified through the contact sheets before this existed."""

    def _verified(self, corpus: pathlib.Path) -> None:
        rows = _manifest(corpus)
        rows[0].update(verified=True, truth="GJ01AB1234", not_a_plate=False, unreadable=False)
        rows[1].update(verified=True, truth=None, not_a_plate=True, unreadable=False)
        rows[2].update(verified=True, truth=None, not_a_plate=False, unreadable=True)
        (corpus / "manifest.jsonl").write_text(
            "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8"
        )

    def test_existing_verifications_become_events(self, corpus: pathlib.Path):
        self._verified(corpus)
        assert seed_from_manifest(corpus) == 3
        events = {e["sighting_id"]: e for e in load_events(corpus)}
        assert events["0"]["verdict"] == "plate" and events["0"]["truth"] == "GJ01AB1234"
        assert events["1"]["verdict"] == "not_a_plate"
        assert events["2"]["verdict"] == "unreadable"

    def test_seeded_labels_are_marked_anchored(self, corpus: pathlib.Path):
        # They were read off a sheet with the pipeline's guess printed under the
        # crop. That is the definition of anchored and the log must say so.
        self._verified(corpus)
        seed_from_manifest(corpus)
        assert all(e["anchored"] for e in load_events(corpus))
        assert all(e["seeded"] for e in load_events(corpus))

    def test_seeding_twice_writes_nothing_the_second_time(self, corpus: pathlib.Path):
        self._verified(corpus)
        seed_from_manifest(corpus)
        assert seed_from_manifest(corpus) == 0
        assert len(load_events(corpus)) == 3

    def test_a_reviewer_is_not_handed_already_verified_work(self, corpus: pathlib.Path):
        self._verified(corpus)
        seed_from_manifest(corpus)
        row, recheck = choose_next(_manifest(corpus), load_events(corpus), "reviewer-a", served=1)
        assert row["sighting_id"] not in {"0", "1", "2"} and recheck is False

    def test_but_the_recheck_measures_them_against_fresh_blind_labels(self, corpus: pathlib.Path):
        self._verified(corpus)
        seed_from_manifest(corpus)
        row, recheck = choose_next(_manifest(corpus), load_events(corpus), "reviewer-a", served=10)
        assert recheck is True and row["sighting_id"] in {"0", "1", "2"}

    def test_an_unverified_manifest_seeds_nothing(self, corpus: pathlib.Path):
        assert seed_from_manifest(corpus) == 0


def test_once_nothing_is_unlabelled_everything_left_is_a_second_opinion():
    # Not "done". There is still useful work — it is just all re-checking, and
    # the reviewer is told that rather than being handed it as fresh crops.
    rows = [crop("0"), crop("1")]
    row, recheck = choose_next(rows, [event("0", "reviewer-b"), event("1", "reviewer-b")],
                               "reviewer-a", served=1)
    assert recheck is True and row is not None


class TestTable:
    """The sheet view. Its risk is the same one the card has: anchoring."""

    def test_an_unlabelled_crop_hides_the_machine_read(self):
        # A table printing the recogniser's guess beside 521 unlabelled
        # thumbnails is the anchoring problem with a scrollbar.
        rows = [crop("0", read="GIIEAFOSE2")]
        table = table_rows(rows, [], "reviewer-a")
        assert "machine_read" not in table[0]
        assert "GIIEAFOSE2" not in json.dumps(table)

    def test_a_crop_i_have_answered_shows_it(self):
        rows = [crop("0", read="GIIEAFOSE2")]
        table = table_rows(rows, [event("0", "reviewer-a")], "reviewer-a")
        assert table[0]["machine_read"] == "GIIEAFOSE2"

    def test_reveal_shows_everything(self):
        # Hidden by default so seeing it is a decision, not policy.
        table = table_rows([crop("0", read="GIIEAFOSE2")], [], "reviewer-a", reveal=True)
        assert table[0]["machine_read"] == "GIIEAFOSE2"

    def test_triage_mode_shows_everything_too(self):
        table = table_rows([crop("0", read="GIIEAFOSE2")], [], "reviewer-a", mode="triage")
        assert table[0]["machine_read"] == "GIIEAFOSE2"

    def test_someone_elses_label_is_listed_without_being_my_own(self):
        rows = [crop("0")]
        table = table_rows(rows, [event("0", "reviewer-b", truth="GJ01AB1234")], "reviewer-a")
        assert table[0]["verdict"] is None
        assert table[0]["others"] == [
            {"labeller": "reviewer-b", "verdict": "plate", "truth": "GJ01AB1234"}
        ]

    def test_a_disagreement_is_flagged_in_the_sheet(self):
        events = [event("0", "reviewer-a", truth="GJ01AB1234"),
                  event("0", "reviewer-b", truth="GJ01AB1284")]
        assert table_rows([crop("0")], events, "reviewer-a")[0]["disputed"] is True

    def test_the_sheet_is_in_queue_order(self):
        rows = [crop("word", read="VIDHYA", format_valid=False, plate_px=200),
                crop("plate", read="GJ01AB1234", format_valid=True, plate_px=40)]
        assert [r["sighting_id"] for r in table_rows(rows, [], "reviewer-a")] == ["plate", "word"]

    def test_every_crop_appears_labelled_or_not(self):
        rows = [crop(str(i)) for i in range(5)]
        assert len(table_rows(rows, [event("0", "reviewer-a")], "reviewer-a")) == 5


class TestExport:
    def test_the_csv_carries_the_machine_read_even_when_unlabelled(self):
        # An export is opened after the fact, not while labelling. Withholding
        # the column would make the file useless for the one comparison it is
        # for.
        text = export_csv([crop("0", read="GIIEAFOSE2")], [], "reviewer-a")
        assert "GIIEAFOSE2" in text

    def test_the_header_is_the_declared_column_order(self):
        text = export_csv([crop("0")], [], "reviewer-a")
        assert text.splitlines()[0] == ",".join(EXPORT_COLUMNS)

    def test_other_labellers_are_flattened_into_one_cell(self):
        text = export_csv([crop("0")], [event("0", "reviewer-b", truth="GJ01AB1234")], "reviewer-a")
        assert "reviewer-b=GJ01AB1234" in text

    def test_a_row_per_crop(self):
        rows = [crop(str(i)) for i in range(4)]
        assert len(export_csv(rows, [], "reviewer-a").strip().splitlines()) == 5  # + header


class TestManualNavigation:
    """Browsing to a crop by name, rather than being handed one by the queue."""

    @pytest.fixture
    def state(self, corpus: pathlib.Path) -> Corpus:
        return Corpus(corpus)

    def test_a_crop_can_be_fetched_by_id(self, state: Corpus):
        row, recheck = state.one("3", "reviewer-a")
        assert row["sighting_id"] == "3" and recheck is False

    def test_an_unknown_id_is_not_a_crash(self, state: Corpus):
        assert state.one("nope", "reviewer-a") == (None, False)

    def test_recheck_is_recomputed_not_taken_from_the_client(self, corpus: pathlib.Path):
        # However a reviewer arrived at this crop, the second-opinion banner
        # has to be honest — so the server decides, not the page.
        (corpus / "labels.jsonl").write_text(
            json.dumps(event("3", "reviewer-b")) + "\n", encoding="utf-8"
        )
        row, recheck = Corpus(corpus).one("3", "reviewer-a")
        assert recheck is True

    def test_my_own_earlier_label_does_not_make_it_a_recheck(self, corpus: pathlib.Path):
        (corpus / "labels.jsonl").write_text(
            json.dumps(event("3", "reviewer-a")) + "\n", encoding="utf-8"
        )
        row, recheck = Corpus(corpus).one("3", "reviewer-a")
        assert recheck is False

    def test_browsing_does_not_consume_the_queue(self, state: Corpus):
        # Manual navigation must not advance the served counter, or looking
        # around would silently shift the every-tenth re-check cadence.
        before = dict(state.served)
        state.one("3", "reviewer-a")
        assert state.served == before


class TestRenameLabeller:
    """The one operation the append-only log cannot express as an append.

    Identity is what the agreement report groups by, so a wrong name is not a
    wrong value a later event can supersede — it is a second annotator who does
    not exist, and it corrupts the one number this tool produces.
    """

    def test_events_move_to_the_new_name(self, corpus: pathlib.Path):
        (corpus / "labels.jsonl").write_text(
            json.dumps(event("0", "wrong-name")) + "\n"
            + json.dumps(event("1", "someone-else")) + "\n",
            encoding="utf-8",
        )
        assert rename_labeller(corpus, "wrong-name", "reviewer-a") == 1
        names = [e["labeller"] for e in load_events(corpus)]
        assert names == ["reviewer-a", "someone-else"]

    def test_the_reattribution_is_recorded_not_silent(self, corpus: pathlib.Path):
        (corpus / "labels.jsonl").write_text(
            json.dumps(event("0", "wrong-name")) + "\n", encoding="utf-8"
        )
        rename_labeller(corpus, "wrong-name", "reviewer-a")
        assert load_events(corpus)[0]["renamed_from"] == "wrong-name"

    def test_a_backup_is_kept(self, corpus: pathlib.Path):
        (corpus / "labels.jsonl").write_text(
            json.dumps(event("0", "wrong-name")) + "\n", encoding="utf-8"
        )
        rename_labeller(corpus, "wrong-name", "reviewer-a")
        backup = (corpus / "labels.jsonl.bak").read_text(encoding="utf-8")
        assert json.loads(backup.splitlines()[0])["labeller"] == "wrong-name"

    def test_a_phantom_second_annotator_disappears(self, corpus: pathlib.Path):
        # One person labelling under two names looks like two people agreeing
        # with each other. That is the failure this exists to undo.
        (corpus / "labels.jsonl").write_text(
            json.dumps(event("0", "wrong-name", truth="GJ01AB1234")) + "\n"
            + json.dumps(event("0", "reviewer-a", truth="GJ01AB1234")) + "\n",
            encoding="utf-8",
        )
        assert agreement(load_events(corpus))["double_labelled"] == 1
        rename_labeller(corpus, "wrong-name", "reviewer-a")
        assert agreement(load_events(corpus))["double_labelled"] == 0

    def test_a_self_disagreement_resolves_to_the_later_answer(self, corpus: pathlib.Path):
        # Their own typo, corrected later under the right name. Once the names
        # match it is one person changing their mind, which is not a dispute.
        (corpus / "labels.jsonl").write_text(
            json.dumps(event("0", "wrong-name", truth="GJ09BH5396")) + "\n"
            + json.dumps(event("0", "reviewer-a", truth="GJ09BM5396")) + "\n",
            encoding="utf-8",
        )
        assert agreement(load_events(corpus))["disputed"] == ["0"]
        rename_labeller(corpus, "wrong-name", "reviewer-a")
        apply_labels(corpus)
        row = next(r for r in _manifest(corpus) if r["sighting_id"] == "0")
        assert "disputed" not in row and row["truth"] == "GJ09BM5396"

    def test_an_unknown_name_changes_nothing(self, corpus: pathlib.Path):
        (corpus / "labels.jsonl").write_text(
            json.dumps(event("0", "reviewer-a")) + "\n", encoding="utf-8"
        )
        assert rename_labeller(corpus, "nobody", "reviewer-a") == 0
        assert not (corpus / "labels.jsonl.bak").exists()


class TestVerifiedPlates:
    """The recogniser's test set: only crops every labeller agreed on."""

    def test_agreement_is_required(self, corpus):
        log = [event("1", "a"), event("1", "b"),                       # agreed plate
               event("2", "a"), event("2", "b", truth="GJ01AB1235"),   # disputed text
               event("3", "a"), event("3", "b", verdict="unreadable"),  # disputed verdict
               event("4", "a", verdict="not_a_plate")]
        (corpus / "labels.jsonl").write_text("".join(json.dumps(e) + "\n" for e in log))
        from scripts.review_server import verified_plates
        assert [(r["sighting_id"], r["truth"]) for r in verified_plates(corpus)] == [
            ("1", "GJ01AB1234")]


class TestFirstCondition:
    """Night first, because 1 of the 41 verified plates was night."""

    def test_the_chosen_condition_comes_first(self):
        rows = [crop("day-plate", read="GJ01AB1234", format_valid=True),
                crop("night-plate", read="GJ01AB1234", format_valid=True)]
        rows[0]["condition"], rows[1]["condition"] = "day", "night"
        row, recheck = choose_next(rows, [], "reviewer-a", served=1, first_condition="night")
        assert row["sighting_id"] == "night-plate" and not recheck

    def test_without_it_the_order_is_unchanged(self):
        rows = [crop("day-plate", read="GJ01AB1234", format_valid=True),
                crop("night-plate", read="GJ01AB1234", format_valid=True)]
        rows[0]["condition"], rows[1]["condition"] = "day", "night"
        assert choose_next(rows, [], "reviewer-a", served=1)[0]["sighting_id"] == "day-plate"


class TestBandFloor:
    """Reads with no digits at all. Measured: 81 labelled, 0 were a plate."""

    def test_a_wordless_read_is_not_served(self):
        rows = [crop("sign", read="VIDHYA", format_valid=False),
                crop("plate", read="GJ01AB1234", format_valid=True)]
        row, _ = choose_next(rows, [], "reviewer-a", served=1)
        assert row["sighting_id"] == "plate"

    def test_the_queue_ends_rather_than_falling_back_to_them(self):
        # "Nothing left worth your time" is the honest answer. Serving 319
        # crops that have never once been a plate is how a reviewer concludes
        # the harvest is broken.
        rows = [crop("sign", read="VIDHYA", format_valid=False)]
        assert choose_next(rows, [], "reviewer-a", served=1) == (None, False)

    def test_min_band_zero_serves_them(self):
        rows = [crop("sign", read="VIDHYA", format_valid=False)]
        row, _ = choose_next(rows, [], "reviewer-a", served=1, min_band=0)
        assert row["sighting_id"] == "sign"

    def test_a_recheck_is_exempt_from_the_floor(self):
        # A re-check compares two people on the same image. Skipping low-band
        # crops there would bias the agreement figure toward easy ones.
        rows = [crop("sign", read="VIDHYA", format_valid=False)]
        events = [event("sign", "reviewer-b", verdict="not_a_plate")]
        row, recheck = choose_next(rows, events, "reviewer-a", served=10)
        assert recheck is True and row["sighting_id"] == "sign"

    def test_they_stay_in_the_corpus_and_in_the_table(self):
        # Kept as evidence: "the pipeline read the camera's own label here" is a
        # useful negative, and deleting rows makes the corpus unauditable.
        rows = [crop("sign", read="VIDHYA", format_valid=False)]
        table = table_rows(rows, [], "reviewer-a")
        assert len(table) == 1 and table[0]["band"] == 0

    def test_the_band_is_exported(self):
        assert "band" in EXPORT_COLUMNS
        text = export_csv([crop("sign", read="VIDHYA", format_valid=False)], [], "reviewer-a")
        assert text.splitlines()[1].split(",")[EXPORT_COLUMNS.index("band")] == "0"
