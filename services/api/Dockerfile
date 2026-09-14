# Shared image for the API, the migration runner and the seed job — they need
# the same dependency set, and one image means one build to wait for on a cold
# `docker compose up`.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencies first so application edits do not invalidate the layer.
#
# fpdf2 carries the detection report (Deliverable 4). Chosen over reportlab at
# 337 kB against several megabytes — this link has been measured at ~132 kB/s
# and has truncated large transfers before — and it embeds the evidence JPEGs
# directly rather than needing them decoded first.
COPY pyproject.toml ./
RUN pip install --no-cache-dir \
        "fastapi>=0.115" \
        "uvicorn[standard]>=0.32" \
        "psycopg[binary,pool]>=3.2" \
        "pydantic>=2.9" \
        "pydantic-settings>=2.6" \
        "httpx>=0.27" \
        "python-multipart>=0.0.12" \
        "fpdf2>=2.8"

COPY services/ ./services/
COPY scripts/ ./scripts/
COPY db/ ./db/

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

# Runs unprivileged: this image reaches the database and the media server, and
# there is no reason for it to be root while doing so.
RUN useradd --create-home --uid 10001 app && chown -R app:app /app
USER app

EXPOSE 8000

CMD ["uvicorn", "services.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
