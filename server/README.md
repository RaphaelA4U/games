# games server

The online part of the `games` library (accounts, friends; later leaderboards and
battles). Design and security rules: `../docs/ONLINE.md`. A JSON API only, every
request signed by a device key; the database holds no secrets.

```
pip install -r requirements.txt pytest httpx textual   # tests also use the library client
PYTHONPATH=.:.. pytest tests
docker compose up -d --build                           # serves on 127.0.0.1:14050
```

Data: one SQLite file in `./data` (back it up; it holds players, devices,
friendships). Secrets (`BEHEER_MELDING_TOKEN`) only in `.env` on the host.
