"""Persist API probe history and turn failed checks into ordinary incidents."""
from datetime import datetime, timezone
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    AlertDestination, ApiConnection, ApiProbeRun, Incident, NotificationDelivery, new_id,
)


def _probe_evidence(checks: list[dict]) -> list[dict]:
    return [{
        "endpoint": urlsplit(check["url"]).path or "/",
        "method": check.get("method", "GET"),
        "status_code": check.get("status_code"),
        "latency_ms": check.get("latency_ms"),
        "error_type": check.get("error_type"),
    } for check in checks]


async def record_api_connection_check(
    session: AsyncSession,
    connection: ApiConnection,
    checks: list[dict],
    status: str,
    *,
    checked_at: datetime | None = None,
) -> None:
    checked_at = checked_at or datetime.now(timezone.utc)
    connection.last_checks = checks
    connection.status = status
    connection.last_checked_at = checked_at
    for check in checks:
        session.add(ApiProbeRun(
            id=new_id(), organization_id=connection.organization_id,
            connection_id=connection.id, endpoint=check["url"],
            status_code=check.get("status_code"), latency_ms=check.get("latency_ms"),
            error_type=check.get("error_type"), checked_at=checked_at,
        ))

    active = await session.scalar(select(Incident).where(
        Incident.organization_id == connection.organization_id,
        Incident.api_connection_id == connection.id,
        Incident.kind == "api_probe", Incident.status != "resolved",
    ).order_by(Incident.started_at.desc()).limit(1))
    if status == "healthy":
        if active:
            active.status = "resolved"
            active.resolved_at = checked_at
        return

    evidence = _probe_evidence(checks)
    high_severity = status == "unreachable" or any(
        check.get("status_code") is None or check.get("status_code", 0) >= 500
        for check in checks
    )
    if active:
        active.evidence = evidence
        active.severity = "high" if high_severity else "medium"
        return

    incident = Incident(
        id=new_id(), organization_id=connection.organization_id,
        service_id=None, api_connection_id=connection.id,
        kind="api_probe", title="Connected API check failed",
        severity="high" if high_severity else "medium",
        status="investigating", evidence=evidence, started_at=checked_at,
    )
    session.add(incident)
    destinations = await session.scalars(select(AlertDestination).where(
        AlertDestination.organization_id == connection.organization_id,
        AlertDestination.enabled.is_(True),
    ))
    for destination in destinations:
        session.add(NotificationDelivery(
            id=new_id(), organization_id=connection.organization_id,
            incident_id=incident.id, destination_id=destination.id,
            status="pending", attempts=0, created_at=checked_at,
        ))
