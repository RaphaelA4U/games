# The games server (docs/ONLINE.md). Small, non-root, read-only at run time.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
COPY requirements.txt /tmp/requirements.txt
RUN pip install --require-hashes -r /tmp/requirements.txt \
    && useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin gamesrv \
    && mkdir /data && chown gamesrv /data
COPY games_server /app/games_server
WORKDIR /app
USER 10001
ENV GAMES_DB=/data/games.db GAMES_SERVER_AUTOSTART=1
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=2)"
CMD ["uvicorn", "games_server.app:app", "--host", "0.0.0.0", "--port", "8080", "--no-server-header", \
     "--no-proxy-headers", "--no-date-header", "--no-access-log", "--limit-concurrency", "200", "--timeout-keep-alive", "5"]
