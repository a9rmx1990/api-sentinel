from datetime import datetime, timedelta, timezone

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.intelligence import (
    anomaly_scores, explain_model, future_failure_label, future_latency_label,
    make_feature_windows, predict_failure, train_failure_model,
)
from app.models import (
    AlertDestination, FeatureWindow, Incident, ModelVersion, NotificationDelivery,
    Organization, Service, Telemetry, new_id,
)


async def analyze_organization(session: AsyncSession, organization_id: str, *, lookback_hours: int = 24) -> dict:
    await session.execute(text("SELECT pg_advisory_xact_lock(hashtext(:org_id))"), {"org_id": organization_id})
    cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours, minutes=15)
    services = list(await session.scalars(select(Service).where(Service.organization_id == organization_id)))
    all_windows, labeled_windows = [], 0
    for service in services:
        events = list(await session.scalars(select(Telemetry).where(
            Telemetry.organization_id == organization_id, Telemetry.service_id == service.id,
            Telemetry.timestamp >= cutoff,
        ).order_by(Telemetry.timestamp)))
        windows = make_feature_windows(events)
        for window, score in zip(windows, anomaly_scores(windows)):
            window["anomaly_score"] = score
            all_windows.append((service, window))
            labeled_windows += future_failure_label(window["window_start"], events) is not None
        if windows:
            rows = [{"id": new_id(), "organization_id": organization_id, "service_id": service.id,
                     "window_start": item["window_start"], "features": item["features"],
                     "anomaly_score": item["anomaly_score"]} for item in windows]
            statement = pg_insert(FeatureWindow).values(rows).on_conflict_do_update(
                index_elements=["service_id", "window_start"],
                set_={"features": pg_insert(FeatureWindow).excluded.features,
                      "anomaly_score": pg_insert(FeatureWindow).excluded.anomaly_score},
            )
            await session.execute(statement)

    latest_by_service = {}
    for service, window in all_windows:
        previous = latest_by_service.get(service.id)
        if previous is None or previous["window_start"] < window["window_start"]:
            latest_by_service[service.id] = window
    model = await session.scalar(select(ModelVersion).where(
        ModelVersion.organization_id == organization_id, ModelVersion.status == "active",
    ).order_by(ModelVersion.trained_at.desc()).limit(1))
    destinations = list(await session.scalars(select(AlertDestination).where(
        AlertDestination.organization_id == organization_id, AlertDestination.enabled.is_(True))))
    created_incidents = 0
    for service in services:
        window = latest_by_service.get(service.id)
        if window is None:
            continue
        features = window["features"]
        risk = predict_failure(features, model.artifact) if model else None
        if risk is not None and risk >= .7:
            kind = "predicted_failure"
        elif features["error_5xx_rate"] >= .05:
            kind = "error_rate"
        elif features["latency_p95"] >= 1500:
            kind = "latency"
        elif window["anomaly_score"] >= .65:
            kind = "anomaly"
        else:
            continue
        existing = await session.scalar(select(Incident.id).where(
            Incident.organization_id == organization_id, Incident.service_id == service.id,
            Incident.kind == kind, Incident.status != "resolved"))
        if existing:
            continue
        evidence = [{"feature": key, "value": value} for key, value in features.items()
                    if key in {"error_5xx_rate", "latency_p95", "traffic_growth", "rolling_5m_error_rate", "rolling_5m_p95"}]
        if risk is not None:
            evidence.append({"feature": "failure_probability", "value": round(risk, 4), "horizon_minutes": 10})
        if kind == "predicted_failure" and model:
            evidence.extend({"feature": row["feature"], "shap_value": row["contribution_log_odds"]}
                            for row in explain_model(features, model.artifact))
        severity = "high" if (risk is not None and risk >= .85) or features["error_5xx_rate"] >= .2 or features["latency_p95"] >= 3000 else "medium"
        title = {"predicted_failure": "Elevated failure risk", "error_rate": "5xx error rate elevated",
                 "latency": "Latency degradation", "anomaly": "Unusual telemetry pattern"}[kind]
        incident_id = new_id()
        session.add(Incident(id=incident_id, organization_id=organization_id, service_id=service.id,
                             kind=kind, title=title, severity=severity, status="investigating", evidence=evidence))
        for destination in destinations:
            session.add(NotificationDelivery(organization_id=organization_id, incident_id=incident_id,
                                             destination_id=destination.id, status="pending"))
        created_incidents += 1

    await session.commit()
    return {"status": "computed", "services": len(services), "feature_windows": len(all_windows),
            "labeled_windows": labeled_windows, "created_incidents": created_incidents}


async def train_organization_model(session: AsyncSession, organization_id: str) -> dict:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24, minutes=10)
    services = list(await session.scalars(select(Service).where(Service.organization_id == organization_id)))
    windows, labels, latency_targets = [], [], []
    for service in services:
        events = list(await session.scalars(select(Telemetry).where(
            Telemetry.organization_id == organization_id, Telemetry.service_id == service.id,
            Telemetry.timestamp >= cutoff,
        ).order_by(Telemetry.timestamp)))
        stored_windows = await session.scalars(select(FeatureWindow).where(
            FeatureWindow.organization_id == organization_id,
            FeatureWindow.service_id == service.id,
            FeatureWindow.window_start >= cutoff + timedelta(minutes=10),
        ).order_by(FeatureWindow.window_start))
        for item in stored_windows:
            window = {"window_start": item.window_start, "features": item.features}
            label = future_failure_label(window["window_start"], events)
            if label is not None:
                windows.append(window)
                labels.append(label)
                latency_targets.append(future_latency_label(window["window_start"], events))
    artifact, metrics = train_failure_model(windows, labels, latency_targets)
    if artifact is None:
        return metrics
    active = metrics.get("quality_status") == "validated"
    if active:
        versions = await session.scalars(select(ModelVersion).where(
            ModelVersion.organization_id == organization_id, ModelVersion.status == "active"))
        for version in versions:
            version.status = "retired"
    version = ModelVersion(organization_id=organization_id, name="failure-risk-xgboost-v1",
                           artifact=artifact, metrics=metrics, status="active" if active else "candidate")
    session.add(version)
    await session.commit()
    return {"model": version.name, "version_status": version.status, **metrics}


async def analyze_all_organizations(session: AsyncSession) -> list[dict]:
    organizations = await session.scalars(select(Organization.id).order_by(Organization.created_at))
    results = []
    for organization_id in organizations:
        await session.execute(text("SELECT set_config('app.organization_id', :org_id, true)"), {"org_id": organization_id})
        results.append({"organization_id": organization_id, **await analyze_organization(session, organization_id, lookback_hours=1)})
    return results
