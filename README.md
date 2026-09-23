# Gaming Leaderboard

A Python/FastAPI REST API for per-game leaderboards backed by Redis sorted sets.
The core score submission, top-player, and player-context endpoints are implemented.

## Ranking rules

- Each game has an independent leaderboard and one best score per player.
- Only a higher submitted score changes the stored score. Repeating a request is safe.
- Equal scores share a competition rank: scores `100, 100, 90` have ranks `1, 1, 3`.
- Display order is score descending, then player ID ascending in ASCII order.
  IDs are case-sensitive; for example, `A` sorts before `a`.
- A top-X limit counts players, not rank groups. Tied players beyond the cutoff are excluded.
- Surroundings are adjacent players in display order, including players with the same rank.
  Near the ends of the leaderboard, fewer neighbors are returned.

**Rank describes performance; display position locates a player in the ordered list.**
They are different when players tie.

## Prerequisites

- Work inside the interview container under `/workspaces/gaming-leaderboard`.
  Keep code and runtime data out of the host Desktop and Downloads directories.
- Python 3.12 or later; the Dockerfile and CI configuration use Python 3.14.
- Dependency installs use the committed `uv.lock` with uv 0.12.18 (bootstrapped below).
- Redis 7 or later. The integration tests require `redis-server` on `PATH`.
- Docker with Compose is optional for running the service stack; a containerized IDE
  does not necessarily provide access to a Docker daemon.

On Ubuntu, install Redis if needed:

```sh
sudo apt-get update
sudo apt-get install -y redis-server
```

## Run locally

### Start or restart the interview demo

After installing dependencies and configuring `.env`, use these commands in the
**VS Code container terminal** (not a Mac terminal):

```sh
cd /workspaces/gaming-leaderboard
.venv/bin/python scripts/local.py start
.venv/bin/python scripts/local.py status
```

The Linux-container helper starts Redis with AOF persistence in `.runtime/redis` and runs
the API in the background with development reload enabled. Forward container port 8000
using VS Code's Ports panel, then open `http://localhost:8000/docs`.

Saving Python source under `src/` reloads the API automatically. After changing `.env`,
configuration, or installed dependencies, restart just the API:

```sh
cd /workspaces/gaming-leaderboard
.venv/bin/python scripts/local.py restart
```

For changed dependencies, sync the updated lockfile before restarting. Logs are in
`.runtime/local/api.log` and `.runtime/local/redis.log`. `scripts/local.py stop` stops only
the API; Redis keeps running. The helper checks process identity before stopping anything
and refuses to take over occupied ports. Do not delete `.runtime/redis`: it holds scores.
This helper is limited to a local, unauthenticated Redis database 0. It refuses a port
change while its Redis is running; migrate endpoints/data deliberately instead of silently
switching stores. Changing the key prefix also selects a different set of leaderboards.
A commit/push alone does not restart anything. This is a workstation demo, not public
production hosting, and it stops if the interview container stops.

### First-time setup or manual foreground launch

Install the pinned lock tool into its own environment, then install the locked project
dependencies. Keeping the tool separate lets `uv sync` clean the application environment:

```sh
cd /workspaces/gaming-leaderboard
python3 -m venv .runtime/uv
.runtime/uv/bin/python -m pip install uv==0.12.18
UV_CACHE_DIR="$PWD/.runtime/uv-cache" .runtime/uv/bin/uv sync --locked --extra dev
source .venv/bin/activate
cp -n .env.example .env
mkdir -p .runtime/redis
```

Generate a unique submission key into your local `.env` without printing it. This preserves
an existing key. The API will not start without a valid key; there is no default or bypass.

```sh
python3 - <<'PY'
from pathlib import Path
import secrets

env = Path(".env")
env.chmod(0o600)
text = env.read_text()
if not any(line.lstrip().startswith("LEADERBOARD_SUBMISSION_API_KEY=")
           for line in text.splitlines()):
    with env.open("a") as output:
        output.write("\nLEADERBOARD_SUBMISSION_API_KEY=" + secrets.token_urlsafe(32) + "\n")
PY
```

Start Redis in a dedicated terminal, using a free local port. This example uses 6379:

```sh
cd /workspaces/gaming-leaderboard
redis-server --bind 127.0.0.1 --port 6379 \
  --dir "$PWD/.runtime/redis" --appendonly yes --appendfsync everysec
```

If a Redis service already owns that port, use it only when appropriate for this project,
or choose another port and update `LEADERBOARD_REDIS_URL` in `.env`.
Do not stop or clear an unrelated Redis instance.

Start the API in another terminal:

```sh
cd /workspaces/gaming-leaderboard
source .venv/bin/activate
uvicorn leaderboard.main:app --reload --no-access-log
```

Open [interactive API documentation](http://localhost:8000/docs). Use **Authorize**, select
`ScoreSubmissionKey`, and enter your local key to submit scores through Swagger UI.
Documentation, leaderboard reads, and health checks remain public. `/metrics` is separately
protected and disabled until configured. Check the process with `GET /health/live`;
`GET /health/ready` also checks Redis.
Stop foreground processes with Ctrl+C. Preserve `.runtime/redis` when restarting Redis.

## Run with Docker Compose

Complete the `.env` and key-generation steps above first. From the project directory,
with a working Docker daemon:

```sh
docker compose up --build
```

The API is available on port 8000. Compose starts Redis 7.4 with append-only persistence
and a named `redis-data` volume. Redis is reachable by the API at `redis://redis:6379/0`;
its port is not published to the host. This Redis is separate from a manually started
local Redis process. Avoid running both API launch methods on port 8000 at once.

Use `docker compose down` to stop the stack while retaining its data volume.
`docker compose down -v` deletes that volume and its leaderboard data.
Compose requires `LEADERBOARD_SUBMISSION_API_KEY` in `.env` or the environment and passes
it to the API. A missing value prevents Compose startup.

## Score submission authentication

`POST /games/{game_id}/scores` requires the `X-API-Key` header. A missing or incorrect key
returns `401` with `{"detail":"Invalid or missing API key"}`. Leaderboard/context GETs,
health checks, `/docs`, and `/openapi.json` remain public. `/metrics` requires its own key.

The configured key must contain 32–256 printable ASCII characters with no whitespace; the generator
above creates a suitable random value. Keep `.env` and the key out of Git and application
logs. A key identifies a trusted game server: anyone holding it can submit scores for any
player in any game. It does not establish player identity or prove scores were earned.
Use HTTPS outside localhost. Rotate the key by replacing its configured value and
restarting the API; all submitting clients must then use the new key.

## API examples

Use a fresh game ID for a clean demonstration. Submitting these same scores again keeps
these players' best scores unchanged. `curl -i` displays the HTTP status as well as JSON.

In the terminal used for these requests, load your locally created `.env` from the project
directory. It contains the generated key; this command does not print it:

```sh
. ./.env
```

Submit three players, including a tie:

```sh
curl -i -X POST http://localhost:8000/games/demo-shared-ranks/scores \
  -H "X-API-Key: $LEADERBOARD_SUBMISSION_API_KEY" \
  -H 'Content-Type: application/json' -d '{"user_id":"alice","score":100}'
curl -i -X POST http://localhost:8000/games/demo-shared-ranks/scores \
  -H "X-API-Key: $LEADERBOARD_SUBMISSION_API_KEY" \
  -H 'Content-Type: application/json' -d '{"user_id":"bob","score":100}'
curl -i -X POST http://localhost:8000/games/demo-shared-ranks/scores \
  -H "X-API-Key: $LEADERBOARD_SUBMISSION_API_KEY" \
  -H 'Content-Type: application/json' -d '{"user_id":"carol","score":90}'
```

A submission returns `200` and fields `game_id`, `user_id`, `score`, `rank`, and `updated`.
`score` is the stored best score. A first submission sets `updated` to `true`; equal or
lower submissions set it to `false` and still return the current best score and rank.

Retrieve the top players:

```sh
curl -i 'http://localhost:8000/games/demo-shared-ranks/leaderboard?limit=10'
```

```json
{
  "game_id": "demo-shared-ranks",
  "entries": [
    {
      "user_id": "alice",
      "score": 100,
      "rank": 1
    },
    {
      "user_id": "bob",
      "score": 100,
      "rank": 1
    },
    {
      "user_id": "carol",
      "score": 90,
      "rank": 3
    }
  ]
}
```

`limit` defaults to 10 and must be 1–100. `limit=1` returns Alice alone, even though Bob
shares rank 1. An empty game returns `200` with `entries: []`.

Retrieve Bob and his neighbors:

```sh
curl -i 'http://localhost:8000/games/demo-shared-ranks/players/bob/context?radius=1'
```

```json
{
  "game_id": "demo-shared-ranks",
  "player": {
    "user_id": "bob",
    "score": 100,
    "rank": 1
  },
  "above": [
    {
      "user_id": "alice",
      "score": 100,
      "rank": 1
    }
  ],
  "below": [
    {
      "user_id": "carol",
      "score": 90,
      "rank": 3
    }
  ]
}
```

`radius` defaults to 1 and must be 0–10. Each neighbor array contains at most `radius`
players, in display order, excluding the requested player. `radius=0` returns empty
neighbor arrays. A player without a score in that game returns `404`.

## Validation and errors

- Score submission requires a valid `X-API-Key`; missing/incorrect keys return `401`.
- Game and player IDs contain 1–64 ASCII letters, digits, underscores, or hyphens.
- Scores are strict integers from 0 through 1,000,000,000. Booleans, numeric strings,
  fractions, nonfinite numbers, and out-of-range scores are rejected.
- Invalid parameters or request bodies return `422` with a `detail` list containing
  each error's `loc`, `msg`, and `type`. Raw invalid input is omitted from that response.
- An unranked player's context returns `404` with
  `{"error":{"code":"PLAYER_NOT_RANKED","message":"Player has not submitted a score for this game."}}`.
- Leaderboard storage failures return `503` with
  `{"error":{"code":"REDIS_UNAVAILABLE","message":"Leaderboard storage is temporarily unavailable."}}`.
- The readiness endpoint uses its existing health-specific error response:
  `503` with `{"detail":"Redis is unavailable"}`. Liveness does not contact Redis.

A timeout does not prove that a submitted score was discarded: Redis may have applied
it before the response was lost. Retry the same submission safely, or read the score
after storage recovers. Highest-score semantics prevent a retry from awarding extra points.

## Configuration

Settings load from environment variables or `.env`:

- `LEADERBOARD_SUBMISSION_API_KEY`: required, with no default; 32–256 printable ASCII
  characters without whitespace. Missing or invalid configuration prevents API startup.
- `LEADERBOARD_METRICS_API_KEY`: optional separate, read-only monitoring credential with
  the same format requirements. Unset/empty disables `/metrics` (404). Configure a different
  value from the submission key (equal keys are rejected); send it only in `X-Metrics-Key`.
- `LEADERBOARD_REDIS_URL`: default `redis://localhost:6379/0`.
- `LEADERBOARD_REDIS_KEY_PREFIX`: default `leaderboard`; change it to isolate deployments.
- `LEADERBOARD_REDIS_CONNECT_TIMEOUT`: default 2 seconds.
- `LEADERBOARD_REDIS_SOCKET_TIMEOUT`: default 2 seconds.
- `LEADERBOARD_APP_NAME`: default `Gaming Leaderboard`.
- `LEADERBOARD_ENVIRONMENT`: default `development`; a label, not a security-mode switch.

Redis connection and socket timeouts bound individual network operations. They are not
an end-to-end HTTP request deadline. Automatic connection/timeout retries are disabled.
Keep credentials out of Git; `.env` and local runtime data are ignored.

## Request tracing and monitoring

Each HTTP response includes a server-generated `X-Request-ID`. Use that ID to find the
corresponding JSON log entry. Incoming request IDs are deliberately replaced. Logs include
the timestamp, route pattern, method, status, and elapsed milliseconds. They omit raw URLs,
query strings, headers, request bodies, actual game/player IDs, and exception messages.
The documented launch command and Docker image disable Uvicorn's raw access log; launching
without `--no-access-log` may additionally log raw paths and query strings.

Redis failures are counted and logged as `connection`, `timeout`, `authentication`,
`response`, or `other`, so a disconnected Redis is distinguishable from a script/configuration
problem. Existing public 503 responses are unchanged. Unexpected errors return a generic
500 `INTERNAL_ERROR` with a request ID; only the exception type is recorded, not its message
or traceback. This deliberately trades detailed debugging context for safe default logs.

After configuring a separate `LEADERBOARD_METRICS_API_KEY` and restarting the API,
`GET /metrics` accepts `X-Metrics-Key` and exposes Prometheus-compatible metrics:

- `leaderboard_http_requests_total`: counts by method, route pattern, and status.
- `leaderboard_http_request_duration_seconds`: latency histogram in seconds.
- `leaderboard_redis_errors_total`: failures by category and operation.

Missing/wrong monitoring credentials return 401 when enabled. Monitoring keys do not grant
write permission. Scrapes are excluded from request metrics/logs. Labels never include
request IDs, raw paths, game IDs, or player IDs; unmatched paths share one `unmatched` label.
These are **per-process** metrics: use one Uvicorn worker per container and scrape each
replica separately. Counters reset on restart. No Prometheus server, dashboard, alerting,
or Redis memory monitoring is deployed by this repository. Keep `/metrics` private behind
your deployment gateway as well as protecting its credential.

## Tests and quality checks

```sh
cd /workspaces/gaming-leaderboard
source .venv/bin/activate
ruff check .
mkdir -p .pytest_cache
pytest --basetemp=.pytest_cache/test-tmp
```

Tests provide their own fake API keys; CI does not need a production key or GitHub secret.
The leaderboard integration tests launch their own real Redis process on an available
loopback port. They do not require the development Redis server to be running. Each test
uses isolated keys; cleanup deletes only its keys, never an existing Redis database.
Pytest replaces the `--basetemp` directory, so keep application data outside that path.

Coverage includes highest-score updates and retries, shared ranks and cutoff ties,
context windows beginning inside a tied group, boundaries, strict validation, game
isolation, concurrent submissions, and storage-failure responses. Storage resilience
checks use dedicated Redis processes for outages, timeouts, recovery, and a normal
restart with AOF persistence; these do not establish crash durability. Observability tests
also cover credential isolation, secret-safe logs, bounded labels, failure categories,
unexpected errors, and concurrent request IDs. Redis must be installed for pytest even
when the development API is run with Compose.

GitHub Actions installs Redis and runs `uv sync --locked --extra dev`, lint, and pytest.
A second job builds the Docker image from the same lockfile and starts a disposable Compose
stack. `scripts/smoke.py` verifies readiness, authentication, best-score preservation,
shared ranks, player context, request IDs, and protected metrics over real HTTP.

To reproduce the Docker check on a machine with Docker Compose supporting `!reset`:

```sh
export LEADERBOARD_SUBMISSION_API_KEY=ci-only-submission-key-not-a-secret-0123456789
export LEADERBOARD_METRICS_API_KEY=ci-only-metrics-key-not-a-secret-0123456789
docker compose -p leaderboard-ci -f compose.yaml -f compose.ci.yaml up --build --wait --wait-timeout 90
docker compose -p leaderboard-ci -f compose.yaml -f compose.ci.yaml exec -T api python - < scripts/smoke.py
docker compose -p leaderboard-ci -f compose.yaml -f compose.ci.yaml down
unset LEADERBOARD_SUBMISSION_API_KEY LEADERBOARD_METRICS_API_KEY
```

Run these commands in a separate test terminal. These are public **test-only** credentials.
The override publishes no ports, attaches no persistent Redis volume, and disables Redis
persistence. Cleanup discards only that test stack's ephemeral scores; it does not touch
the development Redis volume. The smoke script refuses to run unless the environment is
`test`. It is a deployment wiring check, not a load or durability test.

To intentionally update dependencies, run `.runtime/uv/bin/uv lock --upgrade`, sync, review
the lockfile diff, and rerun both test stages. `--locked` fails if project metadata and the
lockfile disagree. Runtime/development dependency versions and hashes are locked; base OS
image tags and isolated build-backend dependencies are not fully pinned, so builds are not
claimed to be bit-for-bit reproducible. Check the actual GitHub workflow result before
submission; local test success alone does not prove the remote workflow has passed.

## Project layout and architecture

- `src/leaderboard/main.py`: application lifecycle and error handlers.
- `src/leaderboard/api/leaderboard.py`: the three leaderboard routes.
- `src/leaderboard/api/system.py`: liveness and readiness routes.
- `src/leaderboard/models.py`: validation and response models.
- `src/leaderboard/storage.py`: Redis operations and atomic scripts.
- `src/leaderboard/config.py`: environment-based settings.
- `src/leaderboard/security.py`: separate score-submission and monitoring credentials.
- `src/leaderboard/observability.py`: safe JSON logs, request IDs, and bounded metrics.
- `tests/`: API, validation, concurrency, and storage integration checks.
- `uv.lock`: resolved application/development dependency versions and hashes.
- `scripts/smoke.py` and `compose.ci.yaml`: isolated packaged-application verification.
- `docs/architecture.md`: request/data-flow diagram and design tradeoffs.

See [the architecture diagram and design notes](docs/architecture.md) for atomic ranking
behavior, performance, persistence, and the scaling path.

Before the interview handoff, push all implementation files and this diagram to your
personal GitHub repository, verify reviewer access and CI, then sign out of personal accounts.
