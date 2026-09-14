"""Onboarding the government grid: the login travels as a reference, never a secret."""

from __future__ import annotations

import importlib

from services.adapters.catalogue import CatalogueCamera, Endpoint


def _camera() -> CatalogueCamera:
    return CatalogueCamera(
        source_id="cam01", name="01 Chiman bhai Bridge",
        endpoints=[Endpoint(protocol="rtsp", url="rtsp://grid.example:8554/stream/cam01")],
    )


def test_the_row_carries_the_credential_reference_when_configured(monkeypatch) -> None:
    monkeypatch.setenv("SENTINEL_CREDENTIAL_REF", "sentinel")
    import scripts.seed_real_feeds as seed
    seed = importlib.reload(seed)
    row = seed._row_for(_camera())
    assert row["credential_ref"] == "sentinel"
    assert "@" not in row["stream_ref"], "credentials must never enter stream_ref"


def test_unset_means_no_reference_and_the_update_keeps_a_manual_one(monkeypatch) -> None:
    monkeypatch.delenv("SENTINEL_CREDENTIAL_REF", raising=False)
    import scripts.seed_real_feeds as seed
    seed = importlib.reload(seed)
    assert seed._row_for(_camera())["credential_ref"] is None
    assert "COALESCE(EXCLUDED.credential_ref, cameras.credential_ref)" in seed._UPSERT


def test_retiring_missing_cameras_never_revives_decommissioned_ones() -> None:
    """A decommissioned camera is a decision; a sync must not undo it."""
    import inspect

    import scripts.seed_real_feeds as seed
    source = inspect.getsource(seed.sync)
    assert "'decommissioned'::camera_status" in source
