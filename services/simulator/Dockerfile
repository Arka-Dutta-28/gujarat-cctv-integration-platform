# Simulator needs ffmpeg; the API image does not, so it gets its own.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# fastapi/uvicorn are here for the relay supervisor, which shares this image
# because it is the one that carries ffmpeg.
RUN pip install --no-cache-dir \
        "psycopg[binary,pool]>=3.2" \
        "pydantic>=2.9" \
        "pydantic-settings>=2.6" \
        "fastapi>=0.115" \
        "uvicorn[standard]>=0.32" \
        "httpx>=0.27"

COPY services/ ./services/
COPY scripts/ ./scripts/

# Derived reference data, versioned with the code and baked into the image.
#
# Every image that normalises a plate must load the *same* confusion table.
# Invariant 3 says normalisation is applied at write time and at query time, so
# an ANPR worker and the API holding different tables would compute different
# join keys, and a plate written by one would not be found by the other. Baking
# the file in is what guarantees they agree; a bind mount would silently revert
# to the built-in prior the first time someone ran without one.
COPY data/ocr-confusion.json ./data/ocr-confusion.json
COPY data/gazetteer ./data/gazetteer

RUN useradd --create-home --uid 10001 app && chown -R app:app /app

# Transcoded profile variants are cached here on a named volume. Creating the
# directory in the image with app ownership matters: Docker seeds a fresh named
# volume from the image path, so the volume comes up writable by this user.
# Without it the container cannot write into the bind-mounted /data, which is
# owned by the host user.
RUN mkdir -p /data/sim-cache && chown -R app:app /data

USER app

CMD ["python", "-m", "services.simulator.simulate"]
