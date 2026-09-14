"""Worker side of the OCR boost: which cameras an operator wants read harder.

/api/ocr-boosts writes requests to camera_ocr_boosts. Each worker process polls
the open ones for its own cameras every few seconds (BoostCache), and each
camera thread swaps its reader when its answer changes
(CameraWorker._apply_boost). The heavy reader is built once per process
(SharedReaders), because a 0.9 B-parameter model per camera would not fit on a
GPU.

The worker writes back what happened, either applied_at or apply_error when the
model will not load, because a boost an operator believes is running on a worker
that cannot run it is worse than no boost.

Like the watchlist cache, a failed poll keeps the last copy: a database blip
must not silently drop boosts mid-investigation, nor start them.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any

log = logging.getLogger("anpr.ocr_boost")

__all__ = ["BoostCache", "SharedReaders", "load_boosts", "report", "REFRESH_S"]

REFRESH_S = float(os.environ.get("ANPR_OCR_BOOST_REFRESH_S", "5"))

_SELECT = """
SELECT DISTINCT ON (camera_id) id, camera_id::text AS camera_id, backend
  FROM camera_ocr_boosts
 WHERE cleared_at IS NULL AND expires_at > now() AND camera_id::text = ANY(%(ids)s)
 ORDER BY camera_id, requested_at DESC
"""


def load_boosts(connect: Any, camera_ids: list[str]) -> dict[str, tuple[int, str]]:
    """camera id -> (boost id, backend), for the open boosts on these cameras."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(_SELECT, {"ids": camera_ids})
        return {r["camera_id"]: (r["id"], r["backend"]) for r in cur.fetchall()}


def report(connect: Any, boost_id: int, error: str | None) -> None:
    """Record whether a boost took effect. Never raises: the camera must keep reading."""
    try:
        with connect() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE camera_ocr_boosts SET applied_at = now(), apply_error = %(e)s"
                " WHERE id = %(id)s",
                {"e": error[:500] if error else None, "id": boost_id},
            )
    except Exception:  # noqa: BLE001
        log.exception("could not record the outcome of OCR boost %s", boost_id)


class BoostCache:
    """The open boosts for one process's cameras, refreshed on a timer."""

    def __init__(self, connect: Any, camera_ids: list[str], refresh_s: float = REFRESH_S,
                 loader: Any = load_boosts) -> None:
        self._connect = connect
        self._ids = camera_ids
        self._refresh_s = refresh_s
        self._loader = loader
        self._boosts: dict[str, tuple[int, str]] = {}
        self._loaded_at: float | None = None
        self._lock = threading.Lock()

    def wanted(self, camera_id: str, now: float | None = None) -> tuple[int, str] | None:
        now = time.monotonic() if now is None else now
        with self._lock:
            if self._loaded_at is None or now - self._loaded_at >= self._refresh_s:
                try:
                    self._boosts = self._loader(self._connect, self._ids)
                except Exception:  # noqa: BLE001 - keep the last good copy
                    log.exception("OCR boost refresh failed; keeping the last copy")
                self._loaded_at = now
            return self._boosts.get(camera_id)


class SharedReaders:
    """One reader per backend per process, loaded on first request.

    Returns `(reader, None)` or `(None, why)`. A backend that failed to load is
    not retried on every frame; it is retried when a *new* boost asks for it,
    after `retry_s`, so a GPU that comes back is picked up.
    """

    def __init__(self, build: Any, retry_s: float = 300.0) -> None:
        self._build = build
        self._retry_s = retry_s
        self._readers: dict[str, Any] = {}
        self._failed: dict[str, tuple[float, str]] = {}
        self._lock = threading.Lock()

    def get(self, backend: str, now: float | None = None) -> tuple[Any, str | None]:
        now = time.monotonic() if now is None else now
        with self._lock:
            if backend in self._readers:
                return self._readers[backend], None
            failed = self._failed.get(backend)
            if failed and now - failed[0] < self._retry_s:
                return None, failed[1]
            try:
                reader = self._build(backend)
                ensure = getattr(reader, "ensure_loaded", None)
                error = ensure() if ensure else None
            except Exception as exc:  # noqa: BLE001
                error = f"{type(exc).__name__}: {exc}"
            if error:
                self._failed[backend] = (now, error)
                log.error("OCR boost reader %s unavailable on this worker: %s", backend, error)
                return None, error
            self._readers[backend] = reader
            self._failed.pop(backend, None)
            return reader, None
