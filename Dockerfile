# syntax=docker/dockerfile:1.7
#
# Multi-stage build for indian-scalper.
#
# Stage 1 ("builder"): install uv, lock-resolve dependencies into a venv
# Stage 2 ("runtime"): slim python-slim with just the venv + source, non-root
#
# Healthcheck hits the dashboard's /health endpoint — if that 200s,
# the scheduler + broker + FastAPI are all live.

# ---------- Stage 1: builder ---------- #
FROM python:3.12-slim-bookworm AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1

# uv produces reproducible lockfile-driven installs. --no-install-recommends
# keeps the builder image small.
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && curl -LsSf https://astral.sh/uv/install.sh | sh
ENV PATH="/root/.local/bin:${PATH}"

WORKDIR /app

# Copy only dependency manifests first so Docker can cache this layer
# across source-code changes.
COPY pyproject.toml uv.lock README.md ./
# --frozen rejects any lockfile drift; --no-dev omits pytest/ruff/mypy.
RUN uv sync --frozen --no-dev --no-install-project

# Copy the app source and re-sync so the project itself is installed (if
# it had an entry point registered; harmless otherwise).
COPY src/ /app/src/
RUN uv sync --frozen --no-dev

# ---------- Stage 2: runtime ---------- #
FROM python:3.12-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:${PATH}" \
    PYTHONPATH="/app/src" \
    DASHBOARD_HOST=0.0.0.0 \
    TZ=Asia/Kolkata

# Create a non-root user + the writable dirs the app expects. These
# match config.yaml's storage + logging paths so mounts line up.
RUN groupadd --system --gid 1001 scalper \
    && useradd --system --uid 1001 --gid scalper --home /app --shell /usr/sbin/nologin scalper \
    && mkdir -p /app/data /app/logs \
    && chown -R scalper:scalper /app

WORKDIR /app

# Virtualenv + source from builder stage.
COPY --from=builder --chown=scalper:scalper /app/.venv /app/.venv
COPY --from=builder --chown=scalper:scalper /app/src /app/src

# pyproject + README are useful for `pip show` / introspection but not
# strictly required at runtime.
COPY --chown=scalper:scalper pyproject.toml README.md ./

USER scalper

EXPOSE 8080

# Uses urllib from stdlib — no extra deps needed. The healthcheck
# considers the container healthy only if /health returns 2xx, which
# exercises the FastAPI app, the broker object, and StateStore.
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request, sys; \
resp = urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=3); \
sys.exit(0 if resp.status == 200 else 1)" || exit 1

CMD ["python", "-m", "serve"]
