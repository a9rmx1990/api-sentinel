"""Add expiring claims so concurrent alert workers cannot duplicate sends."""
from alembic import op
import sqlalchemy as sa

revision = "0005_delivery_claims"
down_revision = "0004_operations_integrations"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("notification_deliveries", sa.Column("claimed_at", sa.DateTime(timezone=True)))


def downgrade():
    op.drop_column("notification_deliveries", "claimed_at")
