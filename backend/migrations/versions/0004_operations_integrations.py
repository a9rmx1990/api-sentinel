"""Scheduled model operations, incident evidence, and webhook delivery state."""
from alembic import op
import sqlalchemy as sa

revision = "0004_operations_integrations"
down_revision = "0003_intelligence_pillars"
branch_labels = None
depends_on = None


def _tenant_table(name, *columns, indexes=(), constraints=()):
    op.create_table(name, *columns, *constraints)
    for column in indexes:
        op.create_index(f"ix_{name}_{column}", name, [column])
    op.execute(f"ALTER TABLE {name} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {name} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_isolation ON {name} "
        "USING (organization_id = current_setting('app.organization_id', true)) "
        "WITH CHECK (organization_id = current_setting('app.organization_id', true))"
    )


def upgrade():
    op.add_column("model_versions", sa.Column("status", sa.String(16), nullable=False, server_default="candidate"))

    _tenant_table(
        "deployment_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("event_id", sa.String(36), nullable=False),
        sa.Column("organization_id", sa.String(36), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("service_id", sa.String(36), sa.ForeignKey("services.id", ondelete="CASCADE"), nullable=False),
        sa.Column("version", sa.String(200), nullable=False),
        sa.Column("commit_sha", sa.String(64)),
        sa.Column("actor", sa.String(120)),
        sa.Column("environment", sa.String(40), nullable=False),
        sa.Column("deployed_at", sa.DateTime(timezone=True), nullable=False),
        indexes=("organization_id", "service_id", "deployed_at"),
        constraints=(sa.UniqueConstraint("service_id", "event_id", name="uq_deployment_service_event"),),
    )
    _tenant_table(
        "application_logs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("event_id", sa.String(36), nullable=False),
        sa.Column("organization_id", sa.String(36), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("service_id", sa.String(36), sa.ForeignKey("services.id", ondelete="CASCADE"), nullable=False),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("level", sa.String(16), nullable=False),
        sa.Column("error_type", sa.String(120)),
        sa.Column("message", sa.String(2000), nullable=False),
        sa.Column("request_id", sa.String(128)),
        sa.Column("trace_id", sa.String(64)),
        indexes=("organization_id", "service_id", "timestamp"),
        constraints=(sa.UniqueConstraint("service_id", "event_id", name="uq_application_log_service_event"),),
    )
    _tenant_table(
        "alert_destinations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("organization_id", sa.String(36), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("encrypted_url", sa.String(4000), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        indexes=("organization_id",),
        constraints=(sa.UniqueConstraint("organization_id", "name", name="uq_alert_destination_org_name"),),
    )
    _tenant_table(
        "notification_deliveries",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("organization_id", sa.String(36), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("incident_id", sa.String(36), sa.ForeignKey("incidents.id", ondelete="CASCADE"), nullable=False),
        sa.Column("destination_id", sa.String(36), sa.ForeignKey("alert_destinations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.String(500)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("delivered_at", sa.DateTime(timezone=True)),
        indexes=("organization_id", "incident_id", "destination_id"),
        constraints=(sa.UniqueConstraint("incident_id", "destination_id", name="uq_delivery_incident_destination"),),
    )


def downgrade():
    for name in ("notification_deliveries", "alert_destinations", "application_logs", "deployment_events"):
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {name}")
        op.drop_table(name)
    op.drop_column("model_versions", "status")
