"""Service ingestion keys, deduplicated telemetry IDs, and safe request metadata."""
from alembic import op
import sqlalchemy as sa

revision = "0002_telemetry_ingestion"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("services", sa.Column("ingest_key_hash", sa.String(64), nullable=True))
    op.create_unique_constraint("uq_services_ingest_key_hash", "services", ["ingest_key_hash"])
    op.execute("DROP POLICY tenant_isolation ON services")
    op.execute("CREATE POLICY tenant_isolation ON services USING (organization_id = current_setting('app.organization_id', true) OR ingest_key_hash = current_setting('app.ingest_key_hash', true)) WITH CHECK (organization_id = current_setting('app.organization_id', true))")
    op.add_column("telemetry", sa.Column("event_id", sa.String(36), nullable=True))
    op.add_column("telemetry", sa.Column("request_size", sa.Integer(), nullable=True))
    op.add_column("telemetry", sa.Column("response_size", sa.Integer(), nullable=True))
    op.add_column("telemetry", sa.Column("request_id", sa.String(128), nullable=True))
    op.add_column("telemetry", sa.Column("trace_id", sa.String(64), nullable=True))
    op.add_column("telemetry", sa.Column("error_type", sa.String(120), nullable=True))
    op.add_column("telemetry", sa.Column("region", sa.String(64), nullable=True))
    op.execute("UPDATE telemetry SET event_id = id WHERE event_id IS NULL")
    op.alter_column("telemetry", "event_id", nullable=False)
    op.create_unique_constraint("uq_telemetry_event_id", "telemetry", ["event_id"])


def downgrade():
    op.drop_constraint("uq_telemetry_event_id", "telemetry", type_="unique")
    for column in ("region", "error_type", "trace_id", "request_id", "response_size", "request_size", "event_id"):
        op.drop_column("telemetry", column)
    op.execute("DROP POLICY tenant_isolation ON services")
    op.drop_constraint("uq_services_ingest_key_hash", "services", type_="unique")
    op.drop_column("services", "ingest_key_hash")
    op.execute("CREATE POLICY tenant_isolation ON services USING (organization_id = current_setting('app.organization_id', true)) WITH CHECK (organization_id = current_setting('app.organization_id', true))")
