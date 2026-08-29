FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src \
    TZ=UTC

RUN useradd --system --uid 10002 --create-home collector \
    && mkdir -p /app/data \
    && chown -R collector:collector /app

RUN python -m pip install --no-cache-dir websockets==15.0.1

WORKDIR /app

# The production host uses Docker's legacy builder, which has incorrectly
# reused a stale COPY layer after source files changed. Tie that layer to a
# content hash supplied by deploy/deploy.sh so a source change must invalidate
# the image while the slower dependency layers can stay cached.
ARG SOURCE_SHA=unknown
RUN printf '%s\n' "$SOURCE_SHA" > /app/.source-sha

COPY --chown=collector:collector src /app/src

USER collector

CMD ["python", "-m", "polytrade_esports", "collect", \
     "--db", "/app/data/esports_live.sqlite3", \
     "--cycles", "0", \
     "--interval-seconds", "60", \
     "--tick-window-hours", "3"]
