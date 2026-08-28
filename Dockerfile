FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src \
    TZ=UTC

RUN useradd --system --uid 10002 --create-home collector \
    && mkdir -p /app/data \
    && chown -R collector:collector /app

WORKDIR /app
COPY --chown=collector:collector src /app/src

USER collector

CMD ["python", "-m", "polytrade_esports", "collect", \
     "--db", "/app/data/esports_live.sqlite3", \
     "--cycles", "0", \
     "--interval-seconds", "60", \
     "--tick-window-hours", "3"]
