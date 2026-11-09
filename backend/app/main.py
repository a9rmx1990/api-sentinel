import os
import asyncio
import hashlib
import hmac
import logging
import secrets
import re
from urllib.parse import urlparse
from pathlib import Path
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

from app.db import SessionFactory, engine, set_ingest_key, set_tenant
from app.models import AlertDestination, ApiConnection, ApiProbeRun, ApplicationLog, DeploymentEvent, FeatureWindow, Incident, Investigation, Membership, ModelVersion, Organization, Runbook, Service, Telemetry, User, new_id
from app.intelligence import explain_model, predict_failure, predict_latency
from app.investigation import investigate
from app.operations import analyze_organization, get_training_readiness, train_organization_model
from app.security import create_session_token, decrypt_secret, encrypt_secret, hash_password, read_session_token, verify_password
from app.notifications import incident_message, post_slack
from app.probes import analyze_api
from app.api_monitoring import record_api_connection_check

app = FastAPI(title="API Sentinel", version="0.3.0")
cors_origins = [
    origin.strip()
    for origin in os.getenv("CORS_ALLOWED_ORIGINS", "http://localhost:5173").split(",")
    if origin.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "DELETE"],
    allow_headers=["Content-Type"],
)
sentinel_key = os.getenv("API_SENTINEL_KEY")
if sentinel_key:
    from api_sentinel import SentinelMiddleware

    app.add_middleware(
        SentinelMiddleware,
        service_key=sentinel_key,
        collector_url=os.getenv("SENTINEL_COLLECTOR_URL", "http://127.0.0.1:8000/api/telemetry/batch"),
        region=os.getenv("SENTINEL_REGION"),
        ignore_paths=("/api/telemetry",),
    )
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "false").lower() == "true"
auth_logger = logging.getLogger("api.auth")


class RegisterInput(BaseModel):
    email: EmailStr
    password: str = Field(min_length=12, max_length=128)
    organization_name: str = Field(min_length=2, max_length=120)


class LoginInput(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)


class TelemetryEventInput(BaseModel):
    event_id: UUID = Field(default_factory=uuid4)
    endpoint: str = Field(min_length=1, max_length=300)
    method: str = Field(min_length=1, max_length=12)
    status_code: int = Field(ge=100, le=599)
    latency_ms: float = Field(ge=0, le=600_000)
    timestamp: datetime | None = None
    request_size: int | None = Field(default=None, ge=0, le=1_000_000_000)
    response_size: int | None = Field(default=None, ge=0, le=1_000_000_000)
    request_id: str | None = Field(default=None, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")
    trace_id: str | None = Field(default=None, max_length=64, pattern=r"^[a-fA-F0-9]+$")
    error_type: str | None = Field(default=None, max_length=120, pattern=r"^[A-Za-z_][A-Za-z0-9_.]*$")
    region: str | None = Field(default=None, max_length=64, pattern=r"^[A-Za-z0-9._-]+$")


class TelemetryBatchInput(BaseModel):
    events: list[TelemetryEventInput] = Field(min_length=1, max_length=100)


class DeploymentInput(BaseModel):
    event_id: UUID = Field(default_factory=uuid4)
    version: str = Field(min_length=1, max_length=200)
    commit_sha: str | None = Field(default=None, max_length=64, pattern=r"^[a-fA-F0-9]{7,64}$")
    actor: str | None = Field(default=None, max_length=120)
    environment: str = Field(default="production", min_length=2, max_length=40)
    deployed_at: datetime | None = None


class ApplicationLogInput(BaseModel):
    event_id: UUID = Field(default_factory=uuid4)
    timestamp: datetime | None = None
    level: str = Field(pattern="^(error|critical|warning)$")
    error_type: str | None = Field(default=None, max_length=120, pattern=r"^[A-Za-z_][A-Za-z0-9_.]*$")
    message: str = Field(min_length=1, max_length=2000)
    request_id: str | None = Field(default=None, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")
    trace_id: str | None = Field(default=None, max_length=64, pattern=r"^[a-fA-F0-9]+$")


class ApplicationLogBatchInput(BaseModel):
    events: list[ApplicationLogInput] = Field(min_length=1, max_length=100)


class SlackDestinationInput(BaseModel):
    name: str = Field(default="Slack", min_length=2, max_length=120)
    webhook_url: str = Field(min_length=30, max_length=1500)


class AlertToggleInput(BaseModel):
    enabled: bool


def valid_slack_webhook(value: str) -> bool:
    try:
        parsed = urlparse(value)
        return (parsed.scheme == "https" and parsed.hostname == "hooks.slack.com"
                and parsed.port in (None, 443) and not parsed.username and not parsed.password
                and parsed.path.startswith("/services/") and not parsed.query and not parsed.fragment)
    except ValueError:
        return False


class RunbookInput(BaseModel):
    title: str = Field(min_length=2, max_length=200)
    body: str = Field(min_length=10, max_length=20_000)


async def get_session():
    async with SessionFactory() as session:
        yield session


async def record_failed_login(email: str) -> int | None:
    import redis

    key_hash = hmac.new(
        os.getenv("SENTINEL_SECRET_KEY", "local-development-secret-change-me").encode(),
        email.encode(), hashlib.sha256,
    ).hexdigest()
    client = redis.Redis.from_url(os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0"))
    script = "local count = redis.call('INCR', KEYS[1]); if count == 1 then redis.call('EXPIRE', KEYS[1], ARGV[1]); end; return count"
    try:
        return await asyncio.to_thread(
            client.eval, script, 1, "sentinel:auth:failed-login:" + key_hash, 300
        )
    except Exception as error:
        auth_logger.warning("Login rate limit unavailable (%s)", type(error).__name__)
        return None
    finally:
        await asyncio.to_thread(client.close)


async def clear_failed_logins(email: str) -> None:
    import redis

    key_hash = hmac.new(
        os.getenv("SENTINEL_SECRET_KEY", "local-development-secret-change-me").encode(),
        email.encode(), hashlib.sha256,
    ).hexdigest()
    client = redis.Redis.from_url(os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0"))
    try:
        await asyncio.to_thread(client.delete, "sentinel:auth:failed-login:" + key_hash)
    except Exception as error:
        auth_logger.warning("Could not clear login rate limit (%s)", type(error).__name__)
    finally:
        await asyncio.to_thread(client.close)


async def get_current_user(request: Request, session: AsyncSession = Depends(get_session)):
    token = request.cookies.get("sentinel_session")
    user_id = read_session_token(token) if token else None
    if not user_id:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Sign in required")
    user = await session.get(User, user_id)
    if not user:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Session is no longer valid")
    membership = await session.scalar(
        select(Membership).where(Membership.user_id == user.id).order_by(Membership.id).limit(1)
    )
    if not membership:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "No workspace membership")
    await set_tenant(session, membership.organization_id)
    organization = await session.get(Organization, membership.organization_id)
    return {"user": user, "membership": membership, "organization": organization, "session": session}


def set_session_cookie(response: Response, user_id: str):
    response.set_cookie(
        "sentinel_session", create_session_token(user_id), httponly=True,
        secure=COOKIE_SECURE, samesite="lax", max_age=12 * 60 * 60, path="/",
    )


@app.get("/api/health")
def health():
    return {"status": "ok", "timestamp": datetime.now(timezone.utc).isoformat()}


@app.get("/api/operations/status")
async def operations_status(context=Depends(get_current_user)):
    import redis
    client = redis.Redis.from_url(os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0"), decode_responses=True)
    try:
        await asyncio.to_thread(client.ping)
        scheduler, worker = await asyncio.gather(
            asyncio.to_thread(client.get, "sentinel:scheduler:heartbeat"),
            asyncio.to_thread(client.get, "sentinel:worker:heartbeat"),
        )
        return {"redis": "connected", "scheduler": "online" if scheduler else "offline",
                "scheduler_last_seen": scheduler, "worker": "online" if worker else "waiting for first job",
                "worker_last_seen": worker}
    except Exception:
        return {"redis": "offline", "scheduler": "offline", "worker": "offline"}
    finally:
        await asyncio.to_thread(client.close)


@app.post("/api/auth/register", status_code=201)
async def register(payload: RegisterInput, response: Response, session: AsyncSession = Depends(get_session)):
    email = payload.email.lower()
    if await session.scalar(select(User.id).where(User.email == email)):
        raise HTTPException(status.HTTP_409_CONFLICT, "An account with this email already exists")
    organization = Organization(id=new_id(), name=payload.organization_name.strip())
    user = User(id=new_id(), email=email, password_hash=hash_password(payload.password))
    session.add_all([organization, user])
    await session.flush()
    await set_tenant(session, organization.id)
    session.add(Membership(user_id=user.id, organization_id=organization.id, role="owner"))
    await session.commit()
    set_session_cookie(response, user.id)
    return {"user": {"id": user.id, "email": user.email}, "organization": {"id": organization.id, "name": organization.name}, "role": "owner"}


@app.post("/api/auth/login")
async def login(payload: LoginInput, response: Response, session: AsyncSession = Depends(get_session)):
    user = await session.scalar(select(User).where(User.email == payload.email.lower()))
    if not user or not verify_password(payload.password, user.password_hash):
        failures = await record_failed_login(payload.email.lower())
        if failures is not None and failures > 5:
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS,
                "Too many sign-in attempts. Try again in five minutes.",
                headers={"Retry-After": "300"},
            )
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid email or password")
    await clear_failed_logins(payload.email.lower())
    set_session_cookie(response, user.id)
    return {"signed_in": True}


@app.post("/api/auth/logout", status_code=204)
def logout(response: Response):
    response.delete_cookie("sentinel_session", path="/")


@app.get("/api/auth/me")
async def me(context=Depends(get_current_user)):
    return {
        "user": {"id": context["user"].id, "email": context["user"].email},
        "organization": {"id": context["organization"].id, "name": context["organization"].name},
        "role": context["membership"].role,
    }


@app.get("/api/services")
async def list_services(context=Depends(get_current_user)):
    session = context["session"]
    rows = await session.scalars(
        select(Service).where(Service.organization_id == context["membership"].organization_id).order_by(Service.name)
    )
    return [{"id": row.id, "name": row.name, "environment": row.environment, "ingest_key_configured": bool(row.ingest_key_hash)} for row in rows]


class ApiConnectionInput(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    base_url: str = Field(min_length=8, max_length=2000)
    openapi_url: str | None = Field(default=None, max_length=2000)
    auth_header: str | None = Field(default=None, max_length=120)
    auth_value: str | None = Field(default=None, max_length=2048)


def api_connection_view(row: ApiConnection) -> dict:
    return {"id": row.id, "name": row.name, "base_url": row.base_url,
            "openapi_url": row.openapi_url, "endpoints": row.endpoints,
            "last_checks": row.last_checks, "status": row.status,
            "last_checked_at": row.last_checked_at.isoformat() if row.last_checked_at else None,
            "created_at": row.created_at.isoformat()}


@app.get("/api/connections")
async def list_api_connections(context=Depends(get_current_user)):
    rows = await context["session"].scalars(select(ApiConnection).where(
        ApiConnection.organization_id == context["membership"].organization_id
    ).order_by(ApiConnection.created_at.desc()))
    return [api_connection_view(row) for row in rows]


@app.post("/api/connections", status_code=201)
async def connect_api(payload: ApiConnectionInput, context=Depends(get_current_user)):
    if context["membership"].role != "owner":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only workspace owners can connect APIs")
    if not payload.name.strip():
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "API name cannot be blank")
    header = payload.auth_header.strip() if payload.auth_header else None
    value = payload.auth_value.strip() if payload.auth_value else None
    if bool(header) != bool(value):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Enter both an auth header name and its value")
    if header and (not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", header) or
                   header.lower() in {"host", "connection", "content-length", "transfer-encoding", "cookie"}):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Enter a valid authentication header name")
    if value and any(char in value for char in "\r\n"):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Authentication value contains invalid characters")
    try:
        result = await analyze_api(payload.base_url, payload.openapi_url, header, value)
    except ValueError as error:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(error)) from error
    row = ApiConnection(
        organization_id=context["membership"].organization_id,
        name=payload.name.strip(), base_url=result["base_url"], openapi_url=result["openapi_url"],
        auth_header=header, encrypted_auth_value=encrypt_secret(value) if value else None,
        endpoints=result["endpoints"], last_checks=result["checks"],
        status=result["status"], last_checked_at=datetime.now(timezone.utc),
    )
    context["session"].add(row)
    await context["session"].flush()
    await record_api_connection_check(
        context["session"], row, result["checks"], result["status"],
        checked_at=row.last_checked_at,
    )
    await context["session"].commit()
    return {**api_connection_view(row), "analysis_note": result["note"]}


@app.delete("/api/connections/{connection_id}", status_code=204)
async def delete_api_connection(connection_id: str, context=Depends(get_current_user)):
    if context["membership"].role != "owner":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only workspace owners can disconnect APIs")
    row = await context["session"].scalar(select(ApiConnection).where(
        ApiConnection.id == connection_id,
        ApiConnection.organization_id == context["membership"].organization_id,
    ))
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "API connection not found")
    await context["session"].delete(row)
    await context["session"].commit()


@app.get("/api/connections/{connection_id}/history")
async def api_connection_history(
    connection_id: str,
    hours: int = Query(default=24, ge=1, le=168),
    context=Depends(get_current_user),
):
    organization_id = context["membership"].organization_id
    connection = await context["session"].scalar(select(ApiConnection).where(
        ApiConnection.id == connection_id, ApiConnection.organization_id == organization_id,
    ))
    if not connection:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "API connection not found")
    rows = await context["session"].scalars(select(ApiProbeRun).where(
        ApiProbeRun.organization_id == organization_id,
        ApiProbeRun.connection_id == connection_id,
        ApiProbeRun.checked_at >= datetime.now(timezone.utc) - timedelta(hours=hours),
    ).order_by(ApiProbeRun.checked_at.desc()).limit(500))
    return [{"endpoint": row.endpoint, "status_code": row.status_code,
             "latency_ms": row.latency_ms, "error_type": row.error_type,
             "checked_at": row.checked_at.isoformat()} for row in rows]


class OrganizationInput(BaseModel):
    name: str = Field(min_length=2, max_length=120)


@app.patch("/api/organizations/current")
async def update_organization(payload: OrganizationInput, context=Depends(get_current_user)):
    if context["membership"].role != "owner":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only workspace owners can change settings")
    organization = context["organization"]
    organization.name = payload.name.strip()
    await context["session"].commit()
    return {"id": organization.id, "name": organization.name}


class ServiceInput(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    environment: str = Field(default="production", min_length=2, max_length=40)


@app.post("/api/services", status_code=201)
async def create_service(payload: ServiceInput, context=Depends(get_current_user)):
    session = context["session"]
    organization_id = context["membership"].organization_id
    ingest_key = "as_live_" + secrets.token_urlsafe(32)
    service = Service(
        organization_id=organization_id, name=payload.name.strip(),
        environment=payload.environment.strip(), ingest_key_hash=hashlib.sha256(ingest_key.encode()).hexdigest(),
    )
    session.add(service)
    await session.commit()
    return {"id": service.id, "name": service.name, "environment": service.environment, "ingest_key": ingest_key}


@app.delete("/api/services/{service_id}", status_code=204)
async def delete_service(service_id: str, context=Depends(get_current_user)):
    if context["membership"].role != "owner":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only workspace owners can delete services")
    session = context["session"]
    service = await session.scalar(select(Service).where(
        Service.id == service_id,
        Service.organization_id == context["membership"].organization_id,
    ))
    if not service:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Service not found")
    await session.delete(service)
    await session.commit()


def hash_ingest_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


async def authenticate_ingest_key(key: str | None, session: AsyncSession) -> Service:
    if not key or not key.startswith("as_live_"):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Valid service ingestion key required")
    key_hash = hash_ingest_key(key)
    await set_ingest_key(session, key_hash)
    service = await session.scalar(select(Service).where(Service.ingest_key_hash == key_hash))
    if not service:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid service ingestion key")
    await set_tenant(session, service.organization_id)
    return service


async def accept_events(payload: TelemetryBatchInput, ingest_key: str | None, session: AsyncSession):
    service = await authenticate_ingest_key(ingest_key, session)
    organization_id = service.organization_id

    rows = []
    for event in payload.events:
        timestamp = event.timestamp or datetime.now(timezone.utc)
        if timestamp.tzinfo is None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Event timestamps must include a timezone")
        rows.append({
            "id": new_id(), "event_id": str(event.event_id), "organization_id": organization_id,
            "service_id": service.id, "endpoint": event.endpoint, "method": event.method.upper(),
            "status_code": event.status_code, "latency_ms": event.latency_ms,
            "request_size": event.request_size, "response_size": event.response_size,
            "request_id": event.request_id, "trace_id": event.trace_id, "error_type": event.error_type,
            "region": event.region,
            "timestamp": timestamp.astimezone(timezone.utc),
        })
    statement = pg_insert(Telemetry).values(rows).on_conflict_do_nothing(index_elements=["event_id"])
    result = await session.execute(statement)
    await session.commit()
    return {"accepted": result.rowcount, "received": len(rows), "duplicates": len(rows) - (result.rowcount or 0)}


@app.post("/api/telemetry", status_code=202)
async def ingest_telemetry(
    payload: TelemetryEventInput,
    x_sentinel_key: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
):
    result = await accept_events(TelemetryBatchInput(events=[payload]), x_sentinel_key, session)
    return {"accepted": result["accepted"] == 1, "duplicates": result["duplicates"]}


@app.post("/api/telemetry/batch", status_code=202)
async def ingest_telemetry_batch(
    payload: TelemetryBatchInput,
    x_sentinel_key: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
):
    return await accept_events(payload, x_sentinel_key, session)


@app.post("/api/deployments", status_code=202)
async def ingest_deployment(
    payload: DeploymentInput,
    x_sentinel_key: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
):
    service = await authenticate_ingest_key(x_sentinel_key, session)
    stamp = payload.deployed_at or datetime.now(timezone.utc)
    if stamp.tzinfo is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Deployment timestamps must include a timezone")
    statement = pg_insert(DeploymentEvent).values({
        "id": new_id(), "event_id": str(payload.event_id), "organization_id": service.organization_id,
        "service_id": service.id, "version": payload.version, "commit_sha": payload.commit_sha,
        "actor": payload.actor, "environment": payload.environment,
        "deployed_at": stamp.astimezone(timezone.utc),
    }).on_conflict_do_nothing(index_elements=["service_id", "event_id"])
    result = await session.execute(statement)
    await session.commit()
    return {"accepted": result.rowcount == 1}


_SECRET_PATTERNS = (
    re.compile(r"(?i)(authorization\s*[:=]\s*bearer\s+)[^\s,;]+"),
    re.compile(r"(?i)((?:password|token|secret|api[_-]?key)\s*[:=]\s*['\"]?)[^\s,'\"]+"),
)


def redact_log_message(message: str) -> str:
    value = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", message)[:2000]
    for pattern in _SECRET_PATTERNS:
        value = pattern.sub(r"\1[REDACTED]", value)
    return value


@app.post("/api/logs/batch", status_code=202)
async def ingest_application_logs(
    payload: ApplicationLogBatchInput,
    x_sentinel_key: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
):
    service = await authenticate_ingest_key(x_sentinel_key, session)
    rows = []
    for event in payload.events:
        stamp = event.timestamp or datetime.now(timezone.utc)
        if stamp.tzinfo is None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Log timestamps must include a timezone")
        rows.append({
            "id": new_id(), "event_id": str(event.event_id), "organization_id": service.organization_id,
            "service_id": service.id, "timestamp": stamp.astimezone(timezone.utc), "level": event.level,
            "error_type": event.error_type, "message": redact_log_message(event.message),
            "request_id": event.request_id, "trace_id": event.trace_id,
        })
    result = await session.execute(pg_insert(ApplicationLog).values(rows).on_conflict_do_nothing(
        index_elements=["service_id", "event_id"]))
    await session.commit()
    return {"accepted": result.rowcount or 0, "received": len(rows), "duplicates": len(rows) - (result.rowcount or 0)}


@app.post("/api/services/{service_id}/ingest-key")
async def rotate_ingest_key(service_id: str, context=Depends(get_current_user)):
    if context["membership"].role != "owner":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only workspace owners can rotate ingestion keys")
    session = context["session"]
    service = await session.scalar(select(Service).where(
        Service.id == service_id,
        Service.organization_id == context["membership"].organization_id,
    ))
    if not service:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Service not found")
    ingest_key = "as_live_" + secrets.token_urlsafe(32)
    service.ingest_key_hash = hash_ingest_key(ingest_key)
    await session.commit()
    return {"ingest_key": ingest_key}


@app.post("/api/intelligence/analyze")
async def analyze_telemetry(context=Depends(get_current_user)):
    organization_id = context["membership"].organization_id
    result = await analyze_organization(context["session"], organization_id)
    result["anomaly_method"] = "Isolation Forest (30+ windows); robust rolling baseline (shorter history)"
    result["message"] = "Feature windows refreshed for the last 24 hours."
    return result


class IncidentStatusInput(BaseModel):
    status: str = Field(pattern="^(investigating|monitoring|resolved)$")


@app.patch("/api/incidents/{incident_id}")
async def update_incident(incident_id: str, payload: IncidentStatusInput, context=Depends(get_current_user)):
    incident = await context["session"].scalar(select(Incident).where(
        Incident.id == incident_id, Incident.organization_id == context["membership"].organization_id))
    if not incident:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Incident not found")
    incident.status = payload.status
    incident.resolved_at = datetime.now(timezone.utc) if payload.status == "resolved" else None
    await context["session"].commit()
    return {"id": incident.id, "status": incident.status, "resolved_at": incident.resolved_at.isoformat() if incident.resolved_at else None}


@app.post("/api/intelligence/train")
async def train_predictor(context=Depends(get_current_user)):
    organization_id = context["membership"].organization_id
    return await train_organization_model(context["session"], organization_id)


@app.get("/api/intelligence")
async def intelligence_summary(context=Depends(get_current_user)):
    session = context["session"]
    organization_id = context["membership"].organization_id
    rows = list(await session.scalars(select(FeatureWindow).where(
        FeatureWindow.organization_id == organization_id,
        FeatureWindow.window_start >= datetime.now(timezone.utc) - timedelta(hours=24),
    ).order_by(FeatureWindow.window_start.desc()).limit(500)))
    services = {row.id: row.name for row in await session.scalars(select(Service).where(Service.organization_id == organization_id))}
    model = await session.scalar(select(ModelVersion).where(
        ModelVersion.organization_id == organization_id, ModelVersion.status == "active",
    ).order_by(ModelVersion.trained_at.desc()).limit(1))
    latest_model = await session.scalar(select(ModelVersion).where(
        ModelVersion.organization_id == organization_id,
    ).order_by(ModelVersion.trained_at.desc()).limit(1))
    latest_by_service = {}
    for row in rows:
        latest_by_service.setdefault(row.service_id, row)
    scores = []
    for service_id, row in latest_by_service.items():
        risk = predict_failure(row.features, model.artifact) if model else None
        latency_forecast = predict_latency(row.features, model.artifact) if model else None
        scores.append({"service_id": service_id, "service": services.get(service_id, "Unknown service"),
                       "window_start": row.window_start.isoformat(), "features": row.features,
                       "anomaly_score": row.anomaly_score, "failure_probability": round(risk, 4) if risk is not None else None,
                       "latency_forecast_ms": round(latency_forecast) if latency_forecast is not None else None,
                       "explanations": explain_model(row.features, model.artifact) if model and risk is not None else []})
    display_model = model or latest_model
    return {"updated_at": datetime.now(timezone.utc).isoformat(), "model": model.name if model else None,
            "model_status": "active" if model else latest_model.status if latest_model else "not_trained",
            "model_metrics": display_model.metrics if display_model else {"status": "not_trained"},
            "training_readiness": await get_training_readiness(session, organization_id),
            "services": scores}


@app.get("/api/runbooks")
async def list_runbooks(context=Depends(get_current_user)):
    rows = await context["session"].scalars(select(Runbook).where(
        Runbook.organization_id == context["membership"].organization_id).order_by(Runbook.created_at.desc()))
    return [{"id": row.id, "title": row.title, "body": row.body, "created_at": row.created_at.isoformat()} for row in rows]


@app.post("/api/runbooks", status_code=201)
async def create_runbook(payload: RunbookInput, context=Depends(get_current_user)):
    row = Runbook(organization_id=context["membership"].organization_id, title=payload.title.strip(), body=payload.body.strip())
    context["session"].add(row)
    await context["session"].commit()
    return {"id": row.id, "title": row.title, "body": row.body, "created_at": row.created_at.isoformat()}


@app.get("/api/alerts")
async def list_alert_destinations(context=Depends(get_current_user)):
    rows = await context["session"].scalars(select(AlertDestination).where(
        AlertDestination.organization_id == context["membership"].organization_id).order_by(AlertDestination.created_at))
    return [{"id": row.id, "name": row.name, "provider": "slack", "enabled": row.enabled,
             "configured": True, "created_at": row.created_at.isoformat()} for row in rows]


@app.post("/api/alerts/slack", status_code=201)
async def create_slack_destination(payload: SlackDestinationInput, context=Depends(get_current_user)):
    if context["membership"].role != "owner":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only workspace owners can configure alerts")
    if not valid_slack_webhook(payload.webhook_url):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Use a Slack incoming webhook URL from hooks.slack.com")
    session = context["session"]
    organization_id = context["membership"].organization_id
    existing = await session.scalar(select(AlertDestination).where(
        AlertDestination.organization_id == organization_id, AlertDestination.name == payload.name.strip()))
    if existing:
        raise HTTPException(status.HTTP_409_CONFLICT, "An alert destination with this name already exists")
    row = AlertDestination(organization_id=organization_id, name=payload.name.strip(),
                           encrypted_url=encrypt_secret(payload.webhook_url))
    session.add(row)
    await session.commit()
    return {"id": row.id, "name": row.name, "provider": "slack", "enabled": row.enabled, "configured": True}


@app.post("/api/alerts/{destination_id}/test")
async def test_alert_destination(destination_id: str, context=Depends(get_current_user)):
    row = await context["session"].scalar(select(AlertDestination).where(
        AlertDestination.id == destination_id,
        AlertDestination.organization_id == context["membership"].organization_id,
        AlertDestination.enabled.is_(True)))
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Enabled alert destination not found")
    try:
        await post_slack(decrypt_secret(row.encrypted_url), "API Sentinel test alert · Workspace webhook is connected.")
    except Exception as error:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Slack delivery failed ({type(error).__name__})") from error
    return {"sent": True}


@app.delete("/api/alerts/{destination_id}", status_code=204)
async def disable_alert_destination(destination_id: str, context=Depends(get_current_user)):
    if context["membership"].role != "owner":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only workspace owners can change alerts")
    row = await context["session"].scalar(select(AlertDestination).where(
        AlertDestination.id == destination_id,
        AlertDestination.organization_id == context["membership"].organization_id))
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Alert destination not found")
    row.enabled = False
    await context["session"].commit()


@app.patch("/api/alerts/{destination_id}")
async def toggle_alert_destination(destination_id: str, payload: AlertToggleInput, context=Depends(get_current_user)):
    if context["membership"].role != "owner":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only workspace owners can change alerts")
    row = await context["session"].scalar(select(AlertDestination).where(
        AlertDestination.id == destination_id,
        AlertDestination.organization_id == context["membership"].organization_id))
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Alert destination not found")
    row.enabled = payload.enabled
    await context["session"].commit()
    return {"id": row.id, "enabled": row.enabled}


@app.post("/api/incidents/{incident_id}/investigate")
async def investigate_incident(incident_id: str, context=Depends(get_current_user)):
    session = context["session"]
    organization_id = context["membership"].organization_id
    incident = await session.scalar(select(Incident).where(Incident.id == incident_id, Incident.organization_id == organization_id))
    if not incident:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Incident not found")
    service = await session.scalar(select(Service).where(
        Service.id == incident.service_id, Service.organization_id == organization_id)) if incident.service_id else None
    api_connection = await session.scalar(select(ApiConnection).where(
        ApiConnection.id == incident.api_connection_id, ApiConnection.organization_id == organization_id
    )) if incident.api_connection_id else None
    docs = list(await session.scalars(select(Runbook).where(Runbook.organization_id == organization_id)))
    deployment_rows = list(await session.scalars(select(DeploymentEvent).where(
        DeploymentEvent.organization_id == organization_id, DeploymentEvent.service_id == incident.service_id,
        DeploymentEvent.deployed_at >= incident.started_at - timedelta(hours=24),
        DeploymentEvent.deployed_at <= incident.started_at,
    ).order_by(DeploymentEvent.deployed_at.desc()).limit(10))) if incident.service_id else []
    log_rows = list(await session.scalars(select(ApplicationLog).where(
        ApplicationLog.organization_id == organization_id, ApplicationLog.service_id == incident.service_id,
        ApplicationLog.timestamp >= incident.started_at - timedelta(minutes=30),
        ApplicationLog.timestamp <= incident.started_at + timedelta(minutes=5),
    ).order_by(ApplicationLog.timestamp.desc()).limit(20))) if incident.service_id else []
    deployments = [{"id": row.id, "version": row.version, "commit_sha": row.commit_sha,
                    "environment": row.environment, "deployed_at": row.deployed_at.isoformat(),
                    "minutes_before": max(0, int((incident.started_at - row.deployed_at).total_seconds() / 60))}
                   for row in deployment_rows]
    logs = [{"id": row.id, "timestamp": row.timestamp.isoformat(), "level": row.level,
             "error_type": row.error_type, "message": row.message, "request_id": row.request_id,
             "trace_id": row.trace_id} for row in log_rows]
    evidence = incident.evidence or []
    report = investigate(title=incident.title, kind=incident.kind,
                         service=service.name if service else api_connection.name if api_connection else "service",
                         evidence=evidence, runbooks=[{"id": doc.id, "title": doc.title, "body": doc.body} for doc in docs],
                         logs=logs, deployments=deployments, incident_started_at=incident.started_at.isoformat())
    row = Investigation(organization_id=organization_id, incident_id=incident.id, report=report)
    session.add(row)
    await session.commit()
    return {"id": row.id, "incident_id": incident.id, "report": report, "created_at": row.created_at.isoformat()}


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0
    ordered = sorted(values)
    return ordered[min(int((len(ordered) - 1) * fraction), len(ordered) - 1)]


@app.get("/api/dashboard")
async def dashboard(range_hours: int = Query(default=1, ge=1, le=24), context=Depends(get_current_user)):
    session = context["session"]
    organization_id = context["membership"].organization_id
    now = datetime.now(timezone.utc)
    window_start = now - timedelta(hours=range_hours)
    service_rows = list(await session.scalars(
        select(Service).where(Service.organization_id == organization_id).order_by(Service.name)
    ))
    event_rows = list(await session.scalars(
        select(Telemetry).where(
            Telemetry.organization_id == organization_id,
            Telemetry.timestamp >= window_start,
        ).order_by(Telemetry.timestamp)
    ))
    recent = event_rows
    minute_events = [event for event in recent if event.timestamp >= now - timedelta(minutes=1)]
    errors = [event for event in recent if event.status_code >= 500]
    latency = [event.latency_ms for event in recent]
    services = []
    for service in service_rows:
        observations = [event for event in recent if event.service_id == service.id]
        failures = sum(event.status_code >= 500 for event in observations)
        error_rate = failures / len(observations) * 100 if observations else 0
        p95 = percentile([event.latency_ms for event in observations], .95)
        services.append({
            "id": service.id, "name": service.name, "environment": service.environment,
            "ingest_key_configured": bool(service.ingest_key_hash),
            "status": "no data" if not observations else "degraded" if error_rate > 1 or p95 > 500 else "healthy",
            "requests_per_minute": sum(event.service_id == service.id for event in minute_events),
            "error_rate": round(error_rate, 2), "p95_latency_ms": round(p95),
            "uptime": round(100 - error_rate, 2),
        })
    traffic = []
    bucket_duration = timedelta(hours=range_hours / 12)
    for bucket in range(11, -1, -1):
        start = now - bucket_duration * (bucket + 1)
        end = start + bucket_duration
        traffic.append({"time": start.strftime("%H:%M"), "requests": sum(start <= event.timestamp < end for event in event_rows)})
    incident_rows = await session.execute(select(Incident, func.coalesce(Service.name, ApiConnection.name))
        .outerjoin(Service, Service.id == Incident.service_id)
        .outerjoin(ApiConnection, ApiConnection.id == Incident.api_connection_id).where(
        Incident.organization_id == organization_id, Incident.status != "resolved").order_by(Incident.started_at.desc()).limit(100))
    incidents = [{"id": incident.id, "title": incident.title, "service": service_name,
                  "severity": incident.severity, "status": incident.status,
                  "started_at": incident.started_at.isoformat()} for incident, service_name in incident_rows]
    api_connections = list(await session.scalars(select(ApiConnection).where(
        ApiConnection.organization_id == organization_id,
        ApiConnection.enabled.is_(True),
    ).order_by(ApiConnection.name)))
    return {
        "updated_at": now.isoformat(),
        "organization": {"id": context["organization"].id, "name": context["organization"].name},
        "summary": {
            "requests_per_minute": len(minute_events),
            "error_rate": round(len(errors) / len(recent) * 100, 2) if recent else 0,
            "p95_latency_ms": round(percentile(latency, .95)),
            "availability": round(100 - len(errors) / len(recent) * 100, 2) if recent else 100,
            "active_incidents": len(incidents),
        },
        "traffic": traffic, "services": services, "incidents": incidents,
        "api_connections": [api_connection_view(row) for row in api_connections],
    }


@app.on_event("shutdown")
async def close_database():
    await engine.dispose()
