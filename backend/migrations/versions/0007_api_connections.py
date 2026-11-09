"""Store tenant-scoped API monitors and encrypted credentials."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0007_api_connections"
down_revision = "0006_runtime_role_grants"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "api_connections",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("organization_id", sa.String(36), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("base_url", sa.String(2000), nullable=False),
        sa.Column("openapi_url", sa.String(2000)),
        sa.Column("auth_header", sa.String(120)),
        sa.Column("encrypted_auth_value", sa.String(4000)),
        sa.Column("endpoints", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("last_checks", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("status", sa.String(24), nullable=False, server_default="checking"),
        sa.Column("last_checked_at", sa.DateTime(timezone=True)),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_api_connections_organization_id", "api_connections", ["organization_id"])
    op.execute("ALTER TABLE api_connections ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE api_connections FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY tenant_isolation ON api_connections "
        "USING (organization_id = current_setting('app.organization_id', true)) "
        "WITH CHECK (organization_id = current_setting('app.organization_id', true))"
    )


def downgrade():
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON api_connections")
    op.drop_index("ix_api_connections_organization_id", table_name="api_connections")
    op.drop_table("api_connections")
