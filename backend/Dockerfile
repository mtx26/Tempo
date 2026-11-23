# Tempo backend image (API, GTFS-RT relay; worker later). Multi-stage, non-root, pinned digests.
#   docker build -t tempo-backend:dev backend
#   docker run ... tempo-backend:dev                                  # API (python -m tempo.api)
#   docker run ... tempo-backend:dev python -m tempo.transport.rt_relay   # relay
# Secrets come from Docker secrets (/run/secrets/...), never from build args or layers.

FROM ghcr.io/astral-sh/uv:0.13.9@sha256:d3b601f0c7b118bc1753febf7c41cc80981df678445e0254284eecf84db79a7d AS uv

FROM python:3.13-slim-bookworm@sha256:a5a7017efc5969e00ae2b91c8eb0631049a569652aa4ffc4501769049ae45c72 AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /src
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project
COPY src ./src
RUN uv sync --locked --no-dev --no-editable

FROM python:3.13-slim-bookworm@sha256:a5a7017efc5969e00ae2b91c8eb0631049a569652aa4ffc4501769049ae45c72
RUN useradd --system --uid 10002 --no-create-home --shell /usr/sbin/nologin tempo
COPY --from=build --chown=root:root /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH" PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER tempo
WORKDIR /tmp
EXPOSE 8000 8090
CMD ["python", "-m", "tempo.api"]
