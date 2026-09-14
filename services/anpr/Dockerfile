# ANPR worker — lean path.
#
# OpenCV and Tesseract from the distribution, not from PyPI. That is not a
# packaging preference: this machine's PyPI throughput is ~68 KB/s and large
# wheels truncate mid-transfer, and more importantly, a stack that installs from
# a distro mirror in seconds is a stack that can be built on an edge box in a
# junction cabinet. The whole edge-first argument is weaker if the analytics
# container is a 3 GB CUDA image.
#
# The learned models (YOLO + a plate-recognition network) live in
# Dockerfile.gpu, which is built on the ultralytics image. Both run the same
# worker; the backends are selected by environment variable, and whichever ran
# is reported alongside the numbers.
# Pinned to 3.13 to match the distribution's Python, and that pin is load-bearing.
#
# Debian builds `python3-opencv` as an extension for *its own* Python, tagged
# with that interpreter's ABI (`cv2.cpython-313-*.so`) and installed under
# /usr/lib/python3/dist-packages. The `python:3.x-slim` images ship a second,
# separate interpreter under /usr/local. Those two only interoperate when their
# versions agree.
#
# On `python:3.12-slim` they no longer do: trixie moved to Python 3.13, so apt
# lands a 3.13 extension that a 3.12 interpreter can neither locate nor load.
# The image builds perfectly and dies at `import cv2` when the worker starts —
# which is the failure this project cares most about, one that looks like
# success right up until it matters.
#
# Two things keep that from recurring silently: the tag below, and the
# `import cv2` assertion at the end of the pip layer, which turns any future
# drift into a failed build instead of a failed deployment.
FROM python:3.13-slim

# PYTHONPATH is where Debian puts python3-opencv. The image's own interpreter
# does not look there by default, and this line is what makes the distro package
# importable without pulling a ~90 MB PyPI wheel over a link measured at 68 KB/s.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    ANPR_MODEL_DIR=/data/models \
    ANPR_VEHICLE_BACKEND=motion \
    ANPR_OCR_BACKEND=auto \
    PYTHONPATH=/usr/lib/python3/dist-packages

# This network intercepts plain HTTP and redirects it to a captive-portal
# login page, so apt over http fails with a truncated index. HTTPS is not
# intercepted. Harmless everywhere else, and a good default regardless: apt
# metadata is signed, but the URLs it fetches should not be readable in transit
# on a police network.
RUN set -eux; \
    sed -i -e 's|http://archive.ubuntu.com|https://archive.ubuntu.com|g' \
           -e 's|http://security.ubuntu.com|https://security.ubuntu.com|g' \
           -e 's|http://deb.debian.org|https://deb.debian.org|g' \
           $(ls /etc/apt/sources.list /etc/apt/sources.list.d/*.sources \
                /etc/apt/sources.list.d/*.list 2>/dev/null) 2>/dev/null || true

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3-opencv \
        # Haar cascades — Debian ships them separately from the Python bindings.
        # A few MB, and the only model file the optional face-analytics module
        # needs; it is what keeps that module from requiring a download at the
        # edge. Absent, the module reports itself unavailable rather than
        # failing, so this stays a soft dependency.
        opencv-data \
        libpq5 \
        tesseract-ocr \
        tesseract-ocr-eng \
        libgl1 \
        libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# A virtualenv *with* system site packages: pip installs stay out of the
# distribution's tree (Debian refuses to mix them, PEP 668), while the distro's
# OpenCV remains importable. Tiny wheels only — this machine's PyPI throughput
# is ~68 KB/s and large wheels truncate mid-transfer.
RUN pip install --no-cache-dir --timeout 60 --retries 10 \
        "psycopg>=3.2" \
        "psycopg-pool>=3.2" \
        "pydantic-settings>=2.5" \
        pytesseract \
    && mkdir -p /data/models /data/crops \
    && python -c "import cv2; print('cv2', cv2.__version__, 'importable at build time')"

# The plate reader, docTR (14 Sep 2026): 63 of 101 night plates against
# Tesseract's 35, and faster on a CPU (37 ms per crop at 4 threads). CPU-only
# PyTorch keeps this an edge image rather than a CUDA one. python-doctr goes in
# with --no-deps so it cannot pull a PyPI OpenCV over the distro build above;
# its runtime dependencies are listed instead. Weights are fetched at build
# time, because a download on first read is a way to fail in front of
# evaluators. Tesseract stays installed: `ANPR_OCR_BACKEND=tesseract` forces it.
ENV ANPR_OCR_THREADS=4
RUN pip install --no-cache-dir --timeout 120 --retries 10 \
        --index-url https://download.pytorch.org/whl/cpu torch torchvision \
    && pip install --no-cache-dir --timeout 120 --retries 10 \
        scipy h5py pypdfium2 pyclipper shapely langdetect rapidfuzz huggingface-hub \
        Pillow anyascii validators tqdm onnx \
    && pip install --no-cache-dir --no-deps python-doctr \
    && python -c "import cv2; from doctr.models import recognition_predictor; recognition_predictor('parseq', pretrained=True); print('docTR parseq ready; cv2', cv2.__version__)"
# Compose sets HOME=/data/models for the model hubs, which would send docTR to
# an empty volume and re-download on every fresh one. Point it at the build copy.
ENV DOCTR_CACHE_DIR=/root/.cache/doctr

COPY services /app/services
# The probe and evaluation tools ship with the image: when plates are not
# being read, the first question is whether it is the pipeline or the
# plumbing, and that has to be answerable inside the container.
COPY scripts /app/scripts

# Derived reference data, versioned with the code and baked into the image.
#
# Every image that normalises a plate must load the *same* confusion table.
# Invariant 3 says normalisation is applied at write time and at query time, so
# an ANPR worker and the API holding different tables would compute different
# join keys, and a plate written by one would not be found by the other. Baking
# the file in is what guarantees they agree; a bind mount would silently revert
# to the built-in prior the first time someone ran without one.
COPY data/ocr-confusion.json /app/data/ocr-confusion.json
COPY data/gazetteer /app/data/gazetteer

CMD ["python", "-m", "services.anpr.worker"]
