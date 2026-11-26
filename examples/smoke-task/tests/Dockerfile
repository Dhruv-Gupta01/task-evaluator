# The separate verifier: the same pinned base and hash-locked packages as the task image.
FROM ghcr.io/laude-institute/t-bench/python-3-13:20250620@sha256:236734f0cafcce942ca09316d57236c2273a2b5411e116454a22cf6d718d95f5

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_BREAK_SYSTEM_PACKAGES=1 \
    PYTHONDONTWRITEBYTECODE=1

COPY requirements.txt /opt/requirements.txt
RUN python3 -m pip install --require-hashes --no-deps -r /opt/requirements.txt

COPY . /tests/
RUN chmod +x /tests/test.sh && mkdir -p /app /logs/verifier
WORKDIR /app
