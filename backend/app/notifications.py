from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import and_, or_, select

from app.db import SessionFactory, set_tenant
from app.models import AlertDestination, Incident, NotificationDelivery, Organization, Service
from app.security import decrypt_secret


def incident_message(incident: Incident, service_name: str) -> str:
    return (
        f"API Sentinel · {incident.severity.upper()} incident\n"
        f"{incident.title} — {service_name}\n"
        f"Status: {incident.status} · Kind: {incident.kind}\n"
        f"Started: {incident.started_at.isoformat()} · Incident: {incident.id}"
    )


async def post_slack(url: str, message: str) -> None:
    async with httpx.AsyncClient(timeout=httpx.Timeout(5.0, connect=2.0)) as client:
        response = await client.post(url, json={"text": message})
        response.raise_for_status()
        if response.text.strip().lower() not in {"", "ok"}:
            raise RuntimeError("Slack webhook returned an unexpected response")


async def dispatch_all_pending() -> dict:
    sent = failed = 0
    now = datetime.now(timezone.utc)
    async with SessionFactory() as session:
        organization_ids = await session.scalars(select(Organization.id).order_by(Organization.created_at))
        for organization_id in organization_ids:
            await set_tenant(session, organization_id)
            records = (await session.execute(
                select(NotificationDelivery, AlertDestination, Incident, Service.name)
                .join(AlertDestination, AlertDestination.id == NotificationDelivery.destination_id)
                .join(Incident, Incident.id == NotificationDelivery.incident_id)
                .join(Service, Service.id == Incident.service_id)
                .where(NotificationDelivery.organization_id == organization_id,
                       NotificationDelivery.attempts < 5,
                       or_(NotificationDelivery.status == "pending",
                           and_(NotificationDelivery.status == "sending",
                                NotificationDelivery.claimed_at < now - timedelta(minutes=5))))
                .order_by(NotificationDelivery.created_at)
                .limit(50)
                .with_for_update(skip_locked=True, of=NotificationDelivery)
            )).all()
            for delivery, _, _, _ in records:
                delivery.status = "sending"
                delivery.claimed_at = now
                delivery.attempts += 1
            if records:
                await session.commit()
            for delivery, destination, incident, service_name in records:
                await set_tenant(session, organization_id)
                try:
                    if not destination.enabled:
                        raise RuntimeError("Alert destination is disabled")
                    await post_slack(decrypt_secret(destination.encrypted_url), incident_message(incident, service_name))
                    delivery.status = "sent"
                    delivery.delivered_at = datetime.now(timezone.utc)
                    delivery.claimed_at = None
                    delivery.last_error = None
                    sent += 1
                except Exception as error:
                    delivery.last_error = type(error).__name__[:500]
                    delivery.claimed_at = None
                    if delivery.attempts >= 5 or not destination.enabled:
                        delivery.status = "failed"
                    else:
                        delivery.status = "pending"
                    failed += 1
                await session.commit()
    return {"sent": sent, "failed_attempts": failed}
