# API Sentinel Phase Completion Handoff

## Purpose

Continue and finish the five-phase improvement plan for API Sentinel. The user approved **all remaining phases** and most recently asked for this handoff so another model can continue with the same context. Do not treat this document as approval to publish, commit, or push. The current worktree is local and uncommitted.

The product goal is a low-friction way to connect a real API, see whether it is responding and what HTTP/latency problems it has, and make the model-training status understandable. Keep active probes distinct from passive request telemetry: a Sentinel-generated GET must not be counted as a customer request or model-training example.

## Project and runtime

- Repository: `/home/a9rmx_1990/crack/api-sentinel`
- Branch: `main`, last pushed commit `296309d`; current changes are uncommitted.
- Stack: FastAPI, PostgreSQL, Redis/Dramatiq, React/TypeScript/Vite.
- Local dashboard normally runs at `http://127.0.0.1:5173`; API at `http://127.0.0.1:8000`.
- PostgreSQL/Redis containers and API, worker, scheduler, and frontend were started earlier in this conversation. Restart API and worker after applying the current migration and edits. Check sessions/processes before starting duplicates.
- `backend/.env` is ignored and mode 600. The user asked to enable `SENTINEL_ALLOW_PRIVATE_API_TARGETS=true` there for local API monitoring. Never print or commit its contents. Production templates keep this setting false.
- Do not include API keys, auth values, cookies, or `.env` contents in tool output or this handoff.

## Product and earlier decisions

The user disliked the old setup that required creating an ingestion key and pasting it into middleware. The new preferred flow is **Services → Connect API**: enter API name and a URL, optionally an OpenAPI JSON URL, then choose no auth, Bearer, API-key header, or custom header. This is active probing: Sentinel makes its own read-only GET requests. It cannot see end-user requests or application exceptions without the optional SDK middleware.

The user also reported `insufficient_data (0 labeled windows)` after pressing Train failure model. Existing model labels need at least five telemetry requests in the ten minutes after a feature window; training needs at least 30 labelable windows, both outcome classes, and a purged chronological split. Active API probes must remain excluded from this training data.

## Five phases and current snapshot

1. **Simplify API connection — implemented.** Connect form, optional same-host OpenAPI JSON parsing, up to eight GET route checks, encrypted auth values, 5-minute scheduled checks, disconnect, Services display, and Overview active-check panel exist. A saved monitor named `test1` was confirmed in the local DB as healthy, HTTP 200, 3.93 ms. Local private targets require the explicit local env setting above.
2. **Credential/URL safety — implementation in progress, unverified.** HTTPS is required for public targets, private/reserved DNS answers are rejected by default, URLs with query/userinfo are rejected, OpenAPI must share the API origin, redirects are not followed, auth is encrypted, and bodies are not stored. `backend/app/probes.py` was just rewritten to resolve and pin each socket connection to a validated IP while keeping the hostname for HTTP Host/TLS SNI. Review that carefully and verify with tests; this rewrite has not yet compiled or run.
3. **Show API problems — implementation in progress.** New `ApiProbeRun` history model, `0008_probe_history_incidents.py`, retention logic, API-probe incident helper, notification joins, and history route/UI have been started. They have not been fully reviewed, migrated, or exercised. Finish failure/recovery lifecycle, recent-history UI, incident listing/investigation, and Slack delivery compatibility.
4. **Model readiness — backend/frontend implementation in progress.** `assess_training_readiness` and collection of recent event/window/label counts were added. The `/api/intelligence` response now includes readiness; frontend shows counts/message and disables training when ineligible. This has not yet been compiled or verified. Fix any issues and ensure 0-label case explains that no passive traffic or too few future requests are the cause, with the next action spelled out.
5. **End-to-end verification — not complete.** The prior local monitor’s 200/3.93 ms result is one success-path observation. Verify status/error paths, probe history, incident creation/recovery, tenant scoping, model-readiness responses, migration, and frontend. No user-owned external production API was provided; don’t claim one was verified.

## Current in-progress code changes

Already modified or added in this worktree:

- `backend/app/probes.py`: replaced HTTPX outbound requests with standard-library address-pinned HTTP/HTTPS connections. Needs syntax/runtime review, particularly TLS SNI/certificate verification, IPv6, request timeout, DNS results, redirects, and URL/path handling.
- `backend/app/models.py`: `ApiProbeRun`; `Incident.service_id` nullable plus `api_connection_id` and exactly-one-source check.
- `backend/migrations/versions/0008_probe_history_incidents.py`: add history table/RLS and incident source. Not yet applied locally.
- `backend/app/api_monitoring.py`: persists probe runs, creates/updates one open incident per failing API monitor, queues enabled Slack destinations, and auto-resolves on healthy recovery.
- `backend/app/tasks.py`: records probe history, processes incidents, and trims history after seven days.
- `backend/app/main.py`: history endpoint, readiness endpoint data, probe incident investigation support, and dashboard incident source changes.
- `backend/app/notifications.py`: notification service-name resolution for either SDK service or API monitor.
- `backend/app/intelligence.py`, `backend/app/operations.py`: readiness counts, reasons, and actionable messages.
- `frontend/src/App.tsx`: readiness panel/button state and history component have been started; finish insertion/visual polish and verify every type.
- `frontend/src/styles.css`: prior connect and active-check styles exist; add styles for probe history and training readiness.
- Earlier Phase 1 docs, compose configuration, env examples, model and UI changes remain part of the same uncommitted work.

## Required remaining work

1. Review and fix the current partial diffs before extending them. Confirm `Incident` existing rows satisfy the new source constraint and that all incident queries, status transitions, investigation, and notification delivery handle either `service_id` or `api_connection_id`.
2. Ensure initial connection checks and scheduled checks both write `ApiProbeRun`; keep only sanitized metadata (status, latency, error class, path), never response body or credential.
3. Complete history API/UI and failure lifecycle. A failed probe should create one tenant-scoped incident and queue configured Slack alerts; repeated failures update it rather than duplicate it; recovery resolves it. Probe incidents should appear in Overview/Incidents and investigate with probe evidence. Keep 7-day history retention bounded.
4. Complete model readiness behavior and UI. Report telemetry event count, analyzed windows, labelable windows, positive/negative labels, minimums, and a plain-language next step. Do not fabricate labels or use active probes for ML. Training should return the same structured readiness instead of a raw `insufficient_data (0 labeled windows)` message.
5. Add focused tests for pinned DNS/SSRF policy, OpenAPI GET discovery, 200/5xx results, no response-body persistence, model label thresholds/readiness reasons, and incident recovery. The user approved completing Phase 5, so verification is in scope.
6. Apply migrations locally, restart API and worker/scheduler as needed, run the focused tests, Python compile, frontend production build, and `git diff --check`. Exercise the existing saved monitor and a controlled local HTTP API returning both 200 and 500. Verify latest status/history/incident and recovery. Avoid sending a real Slack message; test dispatch only with a controlled mock or inspect queued delivery state.
7. Update README and `PROJECT_GUIDE.tex` to describe active checks, their separation from passive traffic/model data, probe retention/incidents, data-readiness rules, and remaining deployment risks. Check production Compose remains private-target-disabled.
8. Give the user a concise phase status and how to use Connect API / how to resolve model readiness. Do not commit or push unless asked.

## Existing caveats to preserve

- Public API URLs must use HTTPS. Private/local target access is opt-in and only appropriate for trusted local development.
- DNS validation must be coupled to the actual pinned connection; avoid validating one resolution and letting the HTTP client resolve the hostname a second time.
- Do not follow redirects, send credentials across origins, accept query-embedded keys, or store API response bodies.
- Active probes provide scheduled availability/status/latency observations, not a complete record of user traffic, error bodies, or proven root cause.
- A predictive model must remain unavailable/candidate until sufficient labeled passive telemetry passes chronological holdout checks. Anomaly detection and deterministic thresholds can still work before then.
