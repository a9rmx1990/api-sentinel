"""Persist active API probe history and support API monitor incidents."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0008_probe_history_incidents"
down_revision = "0007_api_connections"
branch_labels = None
depends_on = None


def upgrade():
    op.alter_column("incidents", "service_id", existing_type=sa.String(36), nullable=True)
    op.add_column("incidents", sa.Column(
        "api_connection_id", sa.String(36),
        sa.ForeignKey("api_connections.id", ondelete="CASCADE"), nullable=True,
    ))
    op.create_index("ix_incidents_api_connection_id", "incidents", ["api_connection_id"])
    op.create_check_constraint(
        "ck_incident_one_source", "incidents",
        "(service_id IS NOT NULL) <> (api_connection_id IS NOT NULL)",
    )

    op.create_table(
        "api_probe_runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("organization_id", sa.String(36), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("connection_id", sa.String(36), sa.ForeignKey("api_connections.id", ondelete="CASCADE"), nullable=False),
        sa.Column("endpoint", sa.String(2000), nullable=False),
        sa.Column("status_code", sa.Integer()),
        sa.Column("latency_ms", sa.Float()),
        sa.Column("error_type", sa.String(120)),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_api_probe_runs_organization_id", "api_probe_runs", ["organization_id"])
    op.create_index("ix_api_probe_runs_connection_id", "api_probe_runs", ["connection_id"])
    op.create_index("ix_api_probe_runs_checked_at", "api_probe_runs", ["checked_at"])
    op.create_index(
        "ix_api_probe_runs_tenant_connection_time", "api_probe_runs",
        ["organization_id", "connection_id", "checked_at"],
    )
    op.execute("ALTER TABLE api_probe_runs ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE api_probe_runs FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY tenant_isolation ON api_probe_runs "
        "USING (organization_id = current_setting('app.organization_id', true)) "
        "WITH CHECK (organization_id = current_setting('app.organization_id', true))"
    )


def downgrade():
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON api_probe_runs")
    op.drop_table("api_probe_runs")
    op.drop_constraint("ck_incident_one_source", "incidents", type_="check")
    op.drop_index("ix_incidents_api_connection_id", table_name="incidents")
    op.drop_column("incidents", "api_connection_id")
    op.alter_column("incidents", "service_id", existing_type=sa.String(36), nullable=False)
