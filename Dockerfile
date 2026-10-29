# radio's image: the package and its dependencies installed from uv.lock into a
# virtual environment in a build stage, then copied onto the same Python image and
# run as distroless's non-root user number, with a read-only root filesystem in
# mind: nothing writes under /app at run time. image.yml builds and smoke-tests it
# on every PR. Both base images are pinned by the digest of their multi-platform
# index (the tag beside it is for people); Renovate bumps the tag and the digest
# together.
#
# Piper's voice joins it with synthesis (paddock#57), fetched at a pinned version
# with its sha256 checked, as v1's Dockerfile does.

FROM ghcr.io/astral-sh/uv:0.13.6@sha256:af22a52f5eafef3d78e551599d405c02df61919d18028f0cea8eda93bc9986d0 AS uv

FROM docker.io/library/python:3.14.10-slim-trixie@sha256:3cabfd7d7aaf394e6040e79fa1eec6ca1c52b74ed9188650472695d9a9326999 AS build
# Bytecode compiled now, since the running image can't write it; the image's own
# Python, never a download.
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PYTHON=/usr/local/bin/python3 \
    UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /src
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
COPY --from=build /app/.venv /app/.venv
COPY LICENSE /app/LICENSE
# JSON logs in the image, text elsewhere (paddock#55). GET /healthz on :8080.
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TWR_LOG_FORMAT=json
# By number, so a runtime can check it isn't root; the same user as the Go images.
USER 65532:65532
EXPOSE 8080
ENTRYPOINT ["radio"]
