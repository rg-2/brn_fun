# brn_fun paper trader — Phase 1 live signal detector, no orders placed.
#
# Two-stage build:
#   1. deps  — install uv + resolve pyproject.toml into /app/.venv
#   2. runtime — copy the venv + source, run as a non-root user
#
# Runtime image is python:3.11-slim, so `docker image ls` shows ~200 MB
# without the data volume attached.

# ----------------------------------------------------------------- 1. deps ---
FROM python:3.11-slim AS deps

# uv is the project's package manager (never fall back to pip/poetry).
RUN pip install --no-cache-dir uv==0.9.30

WORKDIR /app

# Copy only the manifest files so this layer caches when nothing else changed.
COPY pyproject.toml uv.lock README.md ./
COPY src ./src

# Non-editable install of the project + runtime deps (skip dev tools).
RUN uv sync --frozen --no-dev


# ----------------------------------------------------------- 2. runtime -----
FROM python:3.11-slim AS runtime

# Non-root user matching common host UIDs so mounted data/ isn't root-owned.
ARG UID=1000
ARG GID=1000
RUN groupadd -g "${GID}" app && useradd -m -u "${UID}" -g "${GID}" app

WORKDIR /app

# Pull the resolved environment and source from the deps stage.
COPY --from=deps /app/.venv /app/.venv
COPY --from=deps /app/src /app/src
COPY --from=deps /app/pyproject.toml /app/uv.lock /app/README.md ./

# Data + logs + state end up here at runtime (bind-mounted from the host in
# docker-compose so they survive image rebuilds).
RUN mkdir -p /app/data && chown -R app:app /app

USER app

# Put the venv on PATH so `brn` resolves without `uv run` gymnastics.
ENV PATH="/app/.venv/bin:${PATH}"
# Python & uv should log unbuffered so `docker compose logs -f` streams events.
ENV PYTHONUNBUFFERED=1

# Default: run the AUD_USD paper trader with generic paths. Every compose
# service overrides this CMD with strategy-specific --log-file and
# --state-file so the shared image works for any registered strategy.
CMD ["brn", "live", "watch", "audusd", \
     "--log-file", "/app/data/paper_audusd.log", \
     "--state-file", "/app/data/paper_audusd_state.json"]
