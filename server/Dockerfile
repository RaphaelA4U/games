# The games server and its verifier (docs/ONLINE.md). Small, non-root, read-only at run time.
# Build context: the repo root (the server reads games_app/registry.json, the one list of games).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
COPY server/requirements.txt server/requirements-verify.txt /tmp/
# The games themselves (pinned tags, hashed) are only used by the verifier's --verify replays.
RUN pip install --require-hashes -r /tmp/requirements.txt -r /tmp/requirements-verify.txt \
    && useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin gamesrv \
    && mkdir /data && chown gamesrv /data
COPY server/games_server /app/games_server
COPY games_app/registry.json /app/games_server/registry.json
WORKDIR /app
USER 10001
ENV GAMES_DB=/data/games.db GAMES_SERVER_AUTOSTART=1
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=2)"
CMD ["uvicorn", "games_server.app:app", "--host", "0.0.0.0", "--port", "8080", "--no-server-header", \
     "--no-proxy-headers", "--no-date-header", "--no-access-log", "--limit-concurrency", "200", "--timeout-keep-alive", "5"]
