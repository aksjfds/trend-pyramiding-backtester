FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYRAMID_CONFIG=/app/config/okx.toml \
    PYRAMID_STATE_DIR=/data/state \
    PYRAMID_STATE_MOUNT=/data \
    PYRAMID_REQUIRE_PERSISTENT_STATE=true \
    PYRAMID_HEARTBEAT_FILE=/tmp/pyramid-heartbeat.json

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src/ ./src/
COPY config/default.toml config/okx.toml ./config/
RUN pip install . \
    && groupadd --gid 10001 pyramid \
    && useradd --uid 10001 --gid 10001 --no-create-home pyramid \
    && mkdir -p /data/state \
    && chown -R 10001:10001 /data

USER 10001:10001
STOPSIGNAL SIGTERM
HEALTHCHECK --interval=30s --timeout=10s --start-period=120s --retries=3 CMD ["pyramid-health"]

# Northflank runs this as a worker with no exposed/public ports.
# The default is a long-running READ-ONLY check, never an implicit live launch.
CMD ["pyramid-okx", "watch"]
