"""Grant least-privilege runtime access when the deployment app role exists."""
from alembic import op


revision = "0006_runtime_role_grants"
down_revision = "0005_delivery_claims"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        DO $$
        DECLARE
            table_name text;
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'sentinel_app') THEN
                EXECUTE 'GRANT USAGE ON SCHEMA public TO sentinel_app';
                EXECUTE 'GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO sentinel_app';
                FOR table_name IN
                    SELECT tablename FROM pg_tables
                    WHERE schemaname = 'public' AND tablename <> 'alembic_version'
                LOOP
                    EXECUTE format(
                        'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.%I TO sentinel_app',
                        table_name
                    );
                END LOOP;
                EXECUTE format(
                    'ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA public '
                    'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO sentinel_app',
                    current_user
                );
                EXECUTE format(
                    'ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA public '
                    'GRANT USAGE, SELECT ON SEQUENCES TO sentinel_app',
                    current_user
                );
            END IF;
        END
        $$;
    """)


def downgrade():
    op.execute("""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'sentinel_app') THEN
                EXECUTE 'REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM sentinel_app';
                EXECUTE 'REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM sentinel_app';
                EXECUTE 'REVOKE USAGE ON SCHEMA public FROM sentinel_app';
            END IF;
        END
        $$;
    """)
