#!/usr/bin/env bash
# Download and preprocess the Gujarat OSM extract for OSRM.
#
# Not part of `docker compose up`: the extract is a few hundred MB and
# preprocessing takes minutes, and M0-M3 do not need routing. OSRM sits behind
# the `routing` compose profile until M4 wants road-snapped journeys.
#
#   ./infra/osrm/prepare.sh
#   docker compose --profile routing up -d osrm

set -euo pipefail

DATA_DIR="${DATA_DIR:-data/osrm}"
# Geofabrik has no standalone Gujarat extract — it is part of India's Western
# Zone (Gujarat, Maharashtra, Goa, Daman & Diu, Dadra & Nagar Haveli), ~219 MB.
# The old gujarat-latest URL 302s to the Geofabrik homepage, and because that
# redirect answers 200 with HTML, `curl -f` accepts it happily; the failure
# surfaced four steps later as an unreadable osmium PBF error. Hence the
# validation below.
EXTRACT_URL="${EXTRACT_URL:-https://download.geofabrik.de/asia/india/western-zone-latest.osm.pbf}"
PBF="$(basename "$EXTRACT_URL")"
BASE="${PBF%.osm.pbf}"
OSRM_IMAGE="${OSRM_IMAGE:-ghcr.io/project-osrm/osrm-backend:v5.27.1}"

#: A real extract is hundreds of MB. An error page is a few KB.
MIN_PBF_BYTES="${MIN_PBF_BYTES:-10000000}"

mkdir -p "$DATA_DIR"

# A PBF starts with a 4-byte big-endian header length followed by the literal
# "OSMHeader". Checking it costs nothing and turns "downloaded the wrong thing"
# into an error that says so.
is_pbf() {
    [[ -f "$1" ]] || return 1
    [[ "$(head -c 200 "$1" | tr -d '\0' | grep -c OSMHeader || true)" -ge 1 ]]
}

if ! is_pbf "$DATA_DIR/$PBF"; then
    if [[ -f "$DATA_DIR/$PBF" ]]; then
        echo "==> $PBF present but is not an OSM PBF; re-downloading"
        rm -f "$DATA_DIR/$PBF"
    fi
    echo "==> Downloading $EXTRACT_URL"
    # -C - resumes: this is a few hundred MB and the link here is not reliable.
    curl -fL -C - --retry 5 --retry-delay 5 --progress-bar \
        -o "$DATA_DIR/$PBF" "$EXTRACT_URL"
else
    echo "==> $PBF already present and valid, skipping download"
fi

size=$(stat -c%s "$DATA_DIR/$PBF")
if [[ "$size" -lt "$MIN_PBF_BYTES" ]] || ! is_pbf "$DATA_DIR/$PBF"; then
    echo "ERROR: $DATA_DIR/$PBF is $size bytes and does not look like an OSM PBF." >&2
    echo "       The download was probably intercepted or redirected. Check the URL." >&2
    exit 1
fi
echo "==> Extract looks valid ($((size / 1024 / 1024)) MB)"

if [[ -f "$DATA_DIR/$BASE.osrm.mldgr" ]]; then
    echo "==> Extract already processed. Delete $DATA_DIR to rebuild."
    exit 0
fi

# MLD (multi-level Dijkstra) rather than CH: far faster preprocessing, and the
# query latency difference does not matter at our journey volumes.
run() { docker run --rm -v "$(pwd)/$DATA_DIR:/data" "$OSRM_IMAGE" "$@"; }

echo "==> osrm-extract (car profile)"
run osrm-extract -p /opt/car.lua "/data/$PBF"

echo "==> osrm-partition"
run osrm-partition "/data/$BASE.osrm"

echo "==> osrm-customize"
run osrm-customize "/data/$BASE.osrm"

echo "==> Done. Start with: docker compose --profile routing up -d osrm"
