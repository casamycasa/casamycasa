# SpaceBase3000 – presentationsenkät
# En container som både visar formuläret och sparar svaren.
#   Formulär:   http://localhost:8000/
#   API-docs:   http://localhost:8000/docs
#   Svaren:     SQLite-fil i containern, /data/spacebase.db
#
# Bygg och kör:
#   docker build -t spacebase-enkat .
#   docker run -d --name spacebase-enkat -p 8000:8000 spacebase-enkat

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DB_PATH=/data/spacebase.db \
    PORT=8000 \
    OPEN_SESSIONS=true

WORKDIR /srv

# Beroenden först, så att kodändringar inte bygger om pip-lagret
COPY requirements.txt .
RUN pip install -r requirements.txt

# Server och formulär
COPY app/ ./app/

# Kör som vanlig användare. /data ligger i containerns eget filsystem,
# så svaren finns kvar vid omstart och stopp men försvinner med "docker rm".
RUN useradd --system --uid 10001 --home-dir /srv spacebase \
    && mkdir -p /data \
    && chown -R spacebase:spacebase /data
USER spacebase

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import os,urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",\"8000\")}/api/health', timeout=2)" || exit 1

# En worker räcker: SQLite skriver en sak i taget, gott nog för personalenkäter
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips='*'"]
