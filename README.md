# API Sentinel

Minimal API observability dashboard with a FastAPI backend and React + TypeScript
frontend.

## Run locally

Start PostgreSQL from the project root:

```bash
docker compose up -d postgres redis
```

Create the backend environment and install dependencies:

```bash
cd backend
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Set local configuration (the values below match the Compose defaults):

```bash
export DATABASE_URL='postgresql+asyncpg://sentinel:sentinel@localhost:5432/sentinel'
export SENTINEL_SECRET_KEY='replace-with-a-long-random-secret'
export COOKIE_SECURE=false
```

Apply the database migration and start the backend:

```bash
alembic upgrade head
uvicorn app.main:app --reload
```

Run the background worker and minute scheduler in separate terminals from the
`backend` directory:

```bash
.venv/bin/dramatiq app.tasks --processes 1 --threads 2
```

```bash
.venv/bin/python -m app.scheduler
```

The scheduler enqueues feature analysis every minute, connected API checks every
five minutes, and model evaluation once per day. Redis holds the Dramatiq queue;
PostgreSQL remains the source of truth. Redis time-slot locks prevent duplicate
enqueues after a restart.

Start the frontend in another terminal:

```bash
cd frontend
npm install
npm run dev
```

Open the Vite URL shown in the terminal (normally `http://localhost:5173`). The
frontend proxies `/api` requests to FastAPI on port 8000.

Create an account and workspace from the sign-in screen. To get started without
changing your API code, open **Services → Connect API**, enter the API name and
base URL, then optionally add an OpenAPI JSON URL and authentication header.
Choose **Analyze and connect**. Sentinel checks the base URL, discovers up to
eight documented GET routes when an OpenAPI document is available, and repeats
those read-only checks every five minutes while the worker and scheduler run.
Authentication values are encrypted at rest; response bodies are not stored.
The connector rejects redirects and URLs containing credentials or query
parameters. Public targets must use HTTPS. Private or local targets are blocked
by default; enable `SENTINEL_ALLOW_PRIVATE_API_TARGETS=true` only for a trusted,
isolated development deployment.

These checks make requests on Sentinel's schedule. They do not see requests from
your API's users or application-side exceptions. For actual incoming request
traffic and exception types, add the optional SDK middleware below. From the
Services page, choose **Add SDK-monitored service**; the key is shown once.
Install the SDK in the monitored FastAPI application and set that key as
`API_SENTINEL_KEY`:

```bash
pip install -e ./sdk-python
```

```python
import os
from fastapi import FastAPI
from api_sentinel import SentinelMiddleware

app = FastAPI()
app.add_middleware(
    SentinelMiddleware,
    service_key=os.environ["API_SENTINEL_KEY"],
    collector_url="http://127.0.0.1:8000/api/telemetry/batch",
)
```

The SDK sends batches with `X-Sentinel-Key` authentication. It records route
templates, method, status, latency, content lengths, validated request/trace IDs,
and exception type; it does not read request/response bodies or authorization
headers. Its bounded in-memory queue is best-effort: if it fills or the collector
remains unavailable after retries, events are dropped and a count-only warning is
logged. PostgreSQL deduplicates retried events by event ID.

To monitor the Sentinel FastAPI backend itself, provision it for a single-workspace
local installation with `cd backend && .venv/bin/python scripts/provision_self_monitor.py`.
The script creates or rotates the `API Sentinel Backend` service, stores its key
in the ignored, permission-restricted `backend/.env` file, and never prints it.
Install the SDK into the backend environment once with
`backend/.venv/bin/pip install -e ./sdk-python` from the repository root. The app
loads the env file on startup. Its built-in middleware ignores `/api/telemetry`
so collector calls cannot create a feedback loop. `SENTINEL_COLLECTOR_URL` can
override the default collector URL. For multiple workspaces, create the service
from the signed-in UI and set its key in `backend/.env` instead.

Dashboard and telemetry data are scoped to the service's owning workspace. Rotate
an ingestion key from the Services page if it is lost or exposed.
Login failures are throttled per normalized email address in Redis (five failures
per five-minute window). If Redis is unavailable, authentication remains
available and the API logs that the limiter could not be applied.

## Intelligence workflow

In the **Intelligence** view, choose **Analyze last 24 hours** to aggregate
per-minute request counts, error rates, latency percentiles and jitter, traffic
growth, method mix, payload sizes, and rolling 5/15-minute features. Windows and
anomaly scores are stored per workspace. Isolation Forest runs after 30 windows;
short histories use a robust rolling median/MAD baseline.

Choose **Train failure model** to create time-ordered 80/20 training and holdout
sets. XGBoost learns the documented target: at least 5% 5xx errors or p95 above
1500 ms in the following 10 minutes. A separate XGBoost regressor forecasts
future p95 when enough labeled examples exist. The API records Brier score,
PR-AUC, ROC-AUC, and latency MAE where computable. It refuses to train on fewer
than 30 labeled windows or a training set with only one class. Probabilities are
not available until training succeeds; representative failure history is needed
for useful predictions. SHAP contributions describe model attribution and are
not causal claims.

Incidents are created when a current error/latency threshold, anomaly signal, or
validated failure score qualifies. The **Intelligence** view supports a LangGraph
investigation that cites telemetry, ingested application logs and deployment
events, and workspace runbooks. The initial retrieval uses tenant-scoped keyword
matching. Automatic CI/CD and external log-provider sync, vector embeddings, and
an LLM provider are not connected. Reports require human review; no remediation
is executed.

When the worker and scheduler are running, feature analysis and alert delivery
are queued every minute and model evaluation is queued daily. Manual endpoints
are `POST /api/intelligence/analyze` and `POST /api/intelligence/train`; results
are available at `GET /api/intelligence`.
Models remain candidates until a purged chronological holdout has enough examples
from both classes and beats a prevalence/Brier baseline. Only a validated model
is used for predictions. Latency MAE is compared with a constant-mean baseline.

### Synthetic traffic for development

The simulator supports normal traffic, gradual latency degradation, cascading
5xx errors, and flash crowds. It sends explicitly marked `region=synthetic`
events to the service whose key is in `backend/.env`:

```bash
cd backend
.venv/bin/python scripts/simulate_traffic.py --profile cascade --events 10000 --duration-hours 24
```

Synthetic events are stored as telemetry and can create incidents or influence
training labels; use a development service/workspace for these profiles. The
cascade profile distributes traffic across the requested history, so the final
minute can be sparse. To demonstrate incident creation reliably, send a brief
concentrated burst of 5xx requests to a test service before analyzing.

### Deployment and application error evidence

CI can post deployment metadata to `POST /api/deployments` using the service's
`X-Sentinel-Key`. Applications can post sanitized error batches to
`POST /api/logs/batch` with the same header. Log messages are size-limited and
common bearer/password/token patterns are redacted before storage. Neither route
accepts request/response payloads.

### Slack incident alerts

Workspace owners can connect a Slack incoming webhook under **Settings → Slack
incident alerts** and send a test message. The URL is encrypted using the
`SENTINEL_SECRET_KEY`; incident notifications are persisted and delivered by the
worker with bounded retries. Webhooks are restricted to `hooks.slack.com`.

For production, set a unique high-entropy `SENTINEL_SECRET_KEY`, enable secure
cookies behind HTTPS, and use a restricted PostgreSQL application role. Local
development defaults are not production secrets.

## Single-host deployment

The production Compose file builds the API, worker, scheduler, and frontend and
keeps PostgreSQL and Redis off the public host ports. It creates a
`NOSUPERUSER NOBYPASSRLS` application role; migrations run separately with the
database administrator role. Put generated secrets in an ignored
`.env.production` file, then start the stack:

```bash
cp .env.production.example .env.production
openssl rand -hex 32
```

Use separate random outputs for `POSTGRES_ADMIN_PASSWORD`, `APP_DB_PASSWORD`,
and `SENTINEL_SECRET_KEY` in `.env.production`. The two database passwords are
hex-only so they can be used safely in the Compose database URL. Set a domain
and configure a TLS reverse proxy in front of the published web port before
public access; the included Nginx serves HTTP and does not terminate TLS.

```bash
docker compose --env-file .env.production -f docker-compose.production.yml up -d --build
docker compose --env-file .env.production -f docker-compose.production.yml ps
```

Same-origin browser/API requests go through Nginx. If the API is exposed on a
separate origin, set `CORS_ALLOWED_ORIGINS` to the exact comma-separated frontend
origins. Configure host-level monitoring for `/api/health`, review container
logs, and alert on failed health checks and worker/scheduler status in Settings.
The stack has health checks; it does not include a hosted error-monitoring
account.

Create a protected backup on the deployment host:

```bash
./backend/deploy/backup-production.sh /secure-backups/sentinel-$(date +%F).dump
```

Keep backups encrypted and off-host, define a retention policy, and regularly
restore a copy into a disposable database to verify recovery. The backup script
does not encrypt the output itself.

Before release, use a staging deployment and run the workflow in
`PROJECT_GUIDE.tex`: account creation, service ingestion, analysis, incident
investigation, tenant isolation, and alert delivery. A real Slack delivery check
requires a workspace webhook configured in Settings.
