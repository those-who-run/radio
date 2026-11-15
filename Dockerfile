# radio's image: the package and its dependencies installed from uv.lock into a
# virtual environment in a build stage, then copied onto the same Python image and
# run as distroless's non-root user number, with a read-only root filesystem in
# mind: nothing writes under /app at run time. image.yml builds and smoke-tests it
# on every PR. Both base images are pinned by the digest of their multi-platform
# index (the tag beside it is for people); Renovate bumps the tag and the digest
# together.
#
# Piper's voices are baked in (paddock#57 R6, paddock#392): every voice named in
# src/radio/voices/SHA256SUMS, v1's en_GB-alba-medium and
# en_GB-northern_english_male-medium, fetched from the pinned rhasspy/piper-voices
# release in a stage of its own and checked against those sums, as `make voice` does
# locally. radio checks a voice again when it loads it. A deployment picks one by
# name with TWR_RADIO_VOICE; with none set radio speaks with alba. The image sets
# no TWR_RADIO_VOICE_MODEL, since a path would win over the name. The speech cache goes to
# $XDG_CACHE_HOME/twr-radio, /tmp/.cache here: with a read-only root, /tmp is a
# tmpfs or a volume the deployment mounts, never a shared data volume.

FROM ghcr.io/astral-sh/uv:0.13.6@sha256:af22a52f5eafef3d78e551599d405c02df61919d18028f0cea8eda93bc9986d0 AS uv

FROM docker.io/library/python:3.14.10-slim-trixie@sha256:3cabfd7d7aaf394e6040e79fa1eec6ca1c52b74ed9188650472695d9a9326999 AS voice
ARG VOICES_URL=https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0
WORKDIR /voices
COPY src/radio/voices/SHA256SUMS /SHA256SUMS
# A voice's folder in the release follows from its name: en_GB-alba-medium is
# en/en_GB/alba/medium (language, locale, speaker, quality).
RUN python -c "import sys, urllib.request as u; [u.urlretrieve('/'.join((sys.argv[1], loc[:2], loc, speaker, quality, f)), f) for f in (line.split()[1] for line in open('/SHA256SUMS')) for loc, speaker, quality in [f.split('.')[0].split('-')]]" \
        "${VOICES_URL}" \
    && sha256sum -c --strict /SHA256SUMS

FROM docker.io/library/python:3.14.10-slim-trixie@sha256:3cabfd7d7aaf394e6040e79fa1eec6ca1c52b74ed9188650472695d9a9326999 AS build
# Bytecode compiled now, since the running image can't write it; the image's own
# Python, never a download.
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PYTHON=/usr/local/bin/python3 \
    UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /src
# git, for the contracts, which uv installs from their release tag.
RUN apt-get update \
    && apt-get install --yes --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*
# The dependencies first, so a change to the code alone reuses this layer. No dev
# group: the image runs radio, not its checks.
COPY pyproject.toml uv.lock ./
RUN --mount=from=uv,source=/uv,target=/usr/local/bin/uv \
    uv sync --frozen --no-dev --no-install-project
COPY README.md LICENSE ./
COPY src ./src
RUN --mount=from=uv,source=/uv,target=/usr/local/bin/uv \
    uv sync --frozen --no-dev --no-editable

FROM docker.io/library/python:3.14.10-slim-trixie@sha256:3cabfd7d7aaf394e6040e79fa1eec6ca1c52b74ed9188650472695d9a9326999
# The discord sink (paddock#58), as v1's image: ffmpeg resamples each clip to
# Discord's 48 kHz stereo, and discord.py encodes it with libopus.
RUN apt-get update \
    && apt-get install --yes --no-install-recommends ffmpeg libopus0 \
    && rm -rf /var/lib/apt/lists/*
COPY --from=build /app/.venv /app/.venv
COPY --from=voice /voices /app/voices
COPY LICENSE NOTICE /app/
COPY licenses /app/licenses
# JSON logs in the image, text elsewhere (paddock#55). GET /healthz on :8080.
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TWR_LOG_FORMAT=json \
    TWR_RADIO_VOICES_DIR=/app/voices \
    XDG_CACHE_HOME=/tmp/.cache
# By number, so a runtime can check it isn't root; the same user as the Go images.
USER 65532:65532
EXPOSE 8080
ENTRYPOINT ["radio"]
