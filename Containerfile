# Pin the multi-architecture index, including ARMv7, ARM64 and amd64.
FROM docker.io/library/python:3.13-slim-bookworm@sha256:281d39ff02e3b389401586134ff9e6572954687822a58055ec07a60ded4e0b7f

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATABASE_PATH=/data/stock_agent.db

WORKDIR /app
# Metadata-only copies keep builds portable: no pip, apt or target-CPU execution.
# Normalize readability even when SSH extraction uses umask 077.
COPY --chmod=0755 trabe /app/trabe
COPY --chmod=0755 deploy/runtime/ /app/
COPY --chown=1000:1000 --chmod=0700 deploy/data/ /data/
USER 1000:1000
HEALTHCHECK --interval=60s --timeout=10s --start-period=90s --retries=3 CMD ["python", "-m", "trabe", "health"]
CMD ["python", "-m", "trabe", "run"]
