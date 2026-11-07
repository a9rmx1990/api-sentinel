"""Feature windows, model registry, incidents, runbooks, and investigations."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0003_intelligence_pillars"
down_revision = "0002_telemetry_ingestion"
branch_labels = None
depends_on = None


def _tenant_table(name, *columns, indexes=()):
    op.create_table(name, *columns)
    for column in indexes:
        op.create_index(f"ix_{name}_{column}", name, [column])
    op.execute(f"ALTER TABLE {name} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {name} FORCE ROW LEVEL SECURITY")
    op.execute(f"CREATE POLICY tenant_isolation ON {name} USING (organization_id = current_setting('app.organization_id', true)) WITH CHECK (organization_id = current_setting('app.organization_id', true))")


def upgrade():
    _tenant_table(
        "feature_windows",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("organization_id", sa.String(36), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("service_id", sa.String(36), sa.ForeignKey("services.id", ondelete="CASCADE"), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("features", postgresql.JSONB(), nullable=False),
        sa.Column("anomaly_score", sa.Float()),
        sa.UniqueConstraint("service_id", "window_start", name="uq_feature_service_window"),
        indexes=("organization_id", "service_id", "window_start"),
    )
    _tenant_table(
        "model_versions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("organization_id", sa.String(36), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(80), nullable=False),
        sa.Column("trained_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("artifact", postgresql.JSONB(), nullable=False),
        sa.Column("metrics", postgresql.JSONB(), nullable=False),
        indexes=("organization_id",),
    )
    _tenant_table(
        "incidents",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("organization_id", sa.String(36), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("service_id", sa.String(36), sa.ForeignKey("services.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("severity", sa.String(16), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("evidence", postgresql.JSONB(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
        indexes=("organization_id", "service_id", "started_at"),
    )
    _tenant_table(
        "runbooks",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("organization_id", sa.String(36), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("body", sa.String(20000), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        indexes=("organization_id",),
    )
    _tenant_table(
        "investigations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("organization_id", sa.String(36), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("incident_id", sa.String(36), sa.ForeignKey("incidents.id", ondelete="CASCADE"), nullable=False),
        sa.Column("report", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        indexes=("organization_id", "incident_id"),
    )


def downgrade():
    for name in ("investigations", "runbooks", "incidents", "model_versions", "feature_windows"):
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {name}")
        op.drop_table(name)
