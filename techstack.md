# API Sentinel — Ordered Technology Stack

This is the authoritative development order for API Sentinel. Complete and verify
each level before introducing technologies from the next level. A later tool must
not be added merely because it may be needed eventually.

## Non-negotiable rules

1. Build a **modular monolith** first. A module becomes a separate service only
   after a measured scaling, deployment, or ownership need exists.
2. Start with synchronous, correct, observable flows; then make them asynchronous;
   then distribute them.
3. Do not introduce Kafka, Kubernetes, deep learning, or autonomous remediation
   before the preceding levels have a working, tested implementation.
4. Every level must have automated tests and a working local Docker Compose setup
   before moving on.
5. Use PostgreSQL as the source of truth. Redis, Kafka, and observability stores
   are never the source of truth for incidents, users, or configuration.
6. Pin dependency versions in the relevant lock files. Upgrade deliberately, not
   during feature work.

---

## Level 0 — Foundation and engineering hygiene

**Goal:** A reproducible Python and web development environment.

| Area | Technology |
|---|---|
| Python runtime | Python 3.12 |
| Dependency and environment management | `uv` |
| Source control and automation | Git, GitHub Actions |
| Python quality | Ruff, mypy, pytest, pre-commit |
| Frontend runtime | Node.js LTS, npm or pnpm |
| JavaScript quality | TypeScript, ESLint, Prettier, Vitest |
| Local containers | Docker, Docker Compose |

**Exit criteria:** linting, type checks, unit tests, and a minimal Compose command
all run locally and in CI.

**Do not add yet:** databases, Redis, telemetry, ML, queues, or cloud deployment.

---

## Level 1 — Core API and dashboard

**Goal:** A simple API-monitoring application with a usable interface.

| Area | Technology |
|---|---|
| Backend web API | FastAPI, Uvicorn, Pydantic v2 |
| HTTP client | httpx |
| Frontend | React, TypeScript, Vite |
| Client data and navigation | TanStack Query, TanStack Router |
| UI | Tailwind CSS, shadcn/ui |
| Charts | Apache ECharts |
| API contract | FastAPI OpenAPI schema as the source of truth |

Build basic health endpoints and placeholder views for services, telemetry, and
incidents. Keep business logic in Python modules, not route handlers.

**Exit criteria:** a typed frontend can call versioned FastAPI endpoints and show
validated responses.

**Do not add yet:** authentication, database persistence, workers, or ML.

---

## Level 2 — Persistent, secure multi-tenant monitoring

**Goal:** Securely persist organizations, services, telemetry, and incidents.

| Area | Technology |
|---|---|
| Primary database | PostgreSQL |
| ORM and database driver | SQLAlchemy 2 (async), asyncpg |
| Migrations | Alembic |
| Authentication | Email/password with Argon2id hashes and signed HTTP-only cookies for this learning MVP; migrate to OIDC before production |
| Authorization | organization membership checks plus PostgreSQL Row-Level Security |
| Secrets and configuration | environment variables locally; managed secret store in production |

Use PostgreSQL partitions and appropriate indexes for telemetry tables. Every
customer-owned row must have an `organization_id`; every query must be tenant
scoped. Redact secrets and sensitive payload fields before persistence.

**Exit criteria:** migration-driven schema, organization isolation tests, RBAC
tests, service registration, and secure telemetry ingestion all work.

**Do not add yet:** TimescaleDB, vector search, Redis, Kafka, or agent tooling.

---

## Level 3 — Telemetry and deterministic observability

**Goal:** Collect API behavior and calculate trustworthy operational metrics.

| Area | Technology |
|---|---|
| Application instrumentation | OpenTelemetry Python SDK and FastAPI instrumentation |
| Telemetry transport | OpenTelemetry Collector |
| Metrics | Prometheus |
| Metrics dashboard | Grafana |
| Logs | Loki |
| Distributed traces | Tempo |
| Local API SDK | Python SDK built on OpenTelemetry conventions |

Implement request counts, error rate, average latency, P95/P99 latency, and
service health with ordinary deterministic code and SQL. Telemetry ingestion can
remain synchronous at this stage.

**Exit criteria:** one instrumented sample API produces correlated metrics, logs,
and traces; the dashboard displays correct aggregates.

**Do not add yet:** queues, streaming platforms, ML models, or LLMs.

---

## Level 4 — Background work and real-time state

**Goal:** Move durable, non-request work out of the API process without creating
a distributed event platform.

| Area | Technology |
|---|---|
| Cache and short-lived state | Redis |
| Durable background jobs | Dramatiq with Redis broker |
| Scheduled jobs | Dramatiq scheduler or a small explicit scheduler process |
| Rate limiting and distributed locks | Redis |

Use workers for aggregation, notifications, feature calculation, and report
generation. Make each job idempotent and persist its business result in
PostgreSQL. Redis may store windows, locks, caches, and job transport data only.

**Exit criteria:** restarting a worker does not create duplicate incidents or lose
durable work; dashboard requests do not perform expensive aggregation inline.

**Do not add yet:** Kafka or microservices. Redis is not a replacement for the
primary database.

---

## Level 5 — Data analysis and baseline ML

**Goal:** Establish reliable, explainable predictions from historical telemetry.

| Area | Technology |
|---|---|
| Data manipulation | Pandas, NumPy |
| Data-contract validation | Pandera |
| Baselines and anomaly detection | scikit-learn: statistical baselines, Isolation Forest |
| Failure and latency models | XGBoost, scikit-learn calibration tools |
| Explainability | SHAP |
| Experiment and model registry | MLflow |
| Artifacts and training data snapshots | S3-compatible object storage; MinIO locally |

Train chronologically: train → validation → test. Start with threshold and
rolling-average baselines, then Isolation Forest, then XGBoost. Persist feature
definitions, datasets, model metrics, calibration results, and model versions.

**Exit criteria:** models beat documented baselines on temporally held-out data;
feature generation is reproducible; predictions include an explanation and model
version.

**Do not add yet:** PyTorch, autoencoders, LSTMs, Transformers, online training,
or agentic investigation.

---

## Level 6 — ML inference, incidents, and alerting

**Goal:** Turn validated model outputs into useful, auditable operations.

| Area | Technology |
|---|---|
| Model serving | a dedicated FastAPI inference module or worker |
| Incident workflow | PostgreSQL-backed state machine |
| Notifications | webhook delivery first; Slack and email adapters second |
| Reliability controls | idempotency keys, retries with backoff, dead-letter handling |
| Load and integration tests | k6, pytest, Testcontainers |

Keep inference in the modular monolith until it has independently measurable
resource or deployment needs. Anomaly scores, risk scores, and incident severity
are separate fields and must not be conflated.

**Exit criteria:** live telemetry can create one deduplicated incident, explain
the evidence, notify the right audience, and be resolved through an audited
lifecycle.

**Do not add yet:** LangGraph, RAG, remediation execution, Kafka, or Kubernetes.

---

## Level 7 — Evidence-grounded AI investigation

**Goal:** Assist an engineer’s investigation without giving an LLM unchecked
authority.

| Area | Technology |
|---|---|
| Agent orchestration | LangGraph |
| LLM integration and structured output | provider SDK plus Pydantic-validated schemas |
| Knowledge store | PostgreSQL + pgvector |
| Keyword retrieval | PostgreSQL full-text search |
| Retrieval strategy | hybrid vector + keyword + tenant metadata filtering |
| Durable agent state | PostgreSQL-backed LangGraph checkpointing |

Start with one bounded investigation graph: retrieve incident metrics, logs,
traces, and historical incident/runbook evidence; then generate a cited report.
Treat retrieved text and logs as untrusted input. Tool access must be allowlisted,
read-only by default, tenant constrained, and audited.

**Exit criteria:** reports cite their evidence, cannot retrieve another tenant’s
data, resist prompt-injection test cases, and never execute operational actions.

**Do not add yet:** autonomous remediation or multiple independently deployed
agents.

---

## Level 8 — Event streaming and service extraction

**Goal:** Scale only the workloads proven to need independent throughput.

| Area | Technology |
|---|---|
| Event streaming | Apache Kafka |
| Kafka client | `confluent-kafka-python` |
| Schema evolution | JSON Schema initially; Schema Registry when multiple independent producers/consumers exist |
| Service deployment boundary | extract telemetry collector, feature processor, or inference worker only when justified |

Introduce Kafka when measured ingestion volume, consumer lag, replay needs, or
independent scaling make the Redis-worker architecture inadequate. Use stable
event IDs, organization/service partition keys, at-least-once delivery, consumer
idempotency, dead-letter topics, and monitoring for consumer lag.

**Exit criteria:** replaying events cannot duplicate business effects; each
consumer can be scaled independently; event contracts are versioned and tested.

**Do not add yet:** Kubernetes unless the deployment operation now genuinely
requires orchestration.

---

## Level 9 — Production platform and advanced capabilities

**Goal:** Operate the proven platform at production scale.

| Area | Technology |
|---|---|
| Container platform | Kubernetes |
| Kubernetes packaging | Helm |
| Infrastructure as code | Terraform or OpenTofu |
| Production secrets | cloud secret manager or Vault |
| Progressive delivery | rolling deployment first; canary only when required |
| Security scanning | Trivy, dependency scanning, container-image scanning |
| Advanced models, only if baseline gaps remain | PyTorch, autoencoders, temporal neural networks |
| Controlled remediation | policy-bound tools, human approval, audit logging, rollback verification |

Kubernetes does not replace application reliability practices. Preserve health
checks, timeouts, idempotency, audit trails, backups, retention policies, and
tenant isolation from earlier levels.

**Exit criteria:** load-tested scaling, backup/restore validation, disaster
recovery procedure, security assessment, model-drift monitoring, and approval-
gated remediation are all demonstrated.

---

## Technology introduction map

```text
0 Foundation
  ↓
1 FastAPI + React
  ↓
2 PostgreSQL + OIDC + RLS
  ↓
3 OpenTelemetry + Prometheus/Grafana/Loki/Tempo
  ↓
4 Redis + Dramatiq workers
  ↓
5 Pandas/scikit-learn/XGBoost/MLflow
  ↓
6 Inference + incidents + alerts
  ↓
7 LangGraph + pgvector RAG
  ↓
8 Kafka + carefully extracted services
  ↓
9 Kubernetes + advanced ML + approved remediation
```

If a proposed feature requires a technology from a later level, first complete
the missing level or write down the measured reason for making an exception.
