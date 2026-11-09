import asyncio
import os
from datetime import datetime, timedelta, timezone

import dramatiq
from dramatiq.brokers.redis import RedisBroker
from dotenv import load_dotenv
from pathlib import Path

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

from app.db import SessionFactory, engine
from app.models import ApiConnection, ApiProbeRun, Organization
from app.operations import analyze_all_organizations, train_organization_model
from app.db import set_tenant
from sqlalchemy import delete, select
from app.probes import recheck_endpoints
from app.security import decrypt_secret
from app.api_monitoring import record_api_connection_check


broker = RedisBroker(url=os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0"))
dramatiq.set_broker(broker)


def _mark_worker_heartbeat():
    import redis
    client = redis.Redis.from_url(os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0"))
    client.set("sentinel:worker:heartbeat", datetime.now(timezone.utc).isoformat(), ex=120)
    client.close()


async def _analyze_all():
    async with SessionFactory() as session:
        return await analyze_all_organizations(session)


async def _train_all():
    async with SessionFactory() as session:
        organization_ids = await session.scalars(select(Organization.id).order_by(Organization.created_at))
        results = []
        for organization_id in organization_ids:
            await set_tenant(session, organization_id)
            results.append(await train_organization_model(session, organization_id))
        return results


async def _probe_connections():
    async with SessionFactory() as session:
        organization_ids = await session.scalars(select(Organization.id).order_by(Organization.created_at))
        for organization_id in organization_ids:
            await set_tenant(session, organization_id)
            connections = await session.scalars(select(ApiConnection).where(
                ApiConnection.organization_id == organization_id, ApiConnection.enabled.is_(True)))
            for connection in connections:
                try:
                    auth_value = decrypt_secret(connection.encrypted_auth_value) if connection.encrypted_auth_value else None
                    checks, status = await recheck_endpoints(
                        connection.endpoints, connection.auth_header, auth_value)
                except Exception as error:
                    checks = [{"url": connection.base_url, "method": "GET",
                               "status_code": None, "latency_ms": None,
                               "error_type": type(error).__name__}]
                    status = "unreachable"
                await record_api_connection_check(session, connection, checks, status)
            await session.execute(delete(ApiProbeRun).where(
                ApiProbeRun.organization_id == organization_id,
                ApiProbeRun.checked_at < datetime.now(timezone.utc) - timedelta(days=7),
            ))
            await session.commit()


async def _with_dispose(operation):
    try:
        return await operation
    finally:
        await engine.dispose()


@dramatiq.actor(max_retries=5, min_backoff=1_000, max_backoff=60_000, time_limit=300_000)
def analyze_all_organizations_task():
    _mark_worker_heartbeat()
    asyncio.run(_with_dispose(_analyze_all()))


@dramatiq.actor(max_retries=3, min_backoff=60_000, max_backoff=3_600_000, time_limit=900_000)
def train_models_task():
    _mark_worker_heartbeat()
    asyncio.run(_with_dispose(_train_all()))


@dramatiq.actor(max_retries=3, min_backoff=5_000, max_backoff=60_000, time_limit=120_000)
def probe_connected_apis_task():
    _mark_worker_heartbeat()
    asyncio.run(_with_dispose(_probe_connections()))


@dramatiq.actor(max_retries=5, min_backoff=1_000, max_backoff=60_000, time_limit=300_000)
def dispatch_notifications_task():
    _mark_worker_heartbeat()
    from app.notifications import dispatch_all_pending
    asyncio.run(_with_dispose(dispatch_all_pending()))
