"""Provision the Sentinel API as a monitored service for a single-workspace install."""
import asyncio
import hashlib
import os
import secrets
import sys
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import select

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

from app.db import SessionFactory, set_tenant
from app.models import Organization, Service, new_id


async def provision() -> None:
    async with SessionFactory() as session:
        organizations = list(await session.scalars(select(Organization).order_by(Organization.created_at).limit(2)))
        if len(organizations) != 1:
            print(f"Skipped: expected one workspace, found {len(organizations)}. No changes made.")
            return

        organization = organizations[0]
        await set_tenant(session, organization.id)
        service = await session.scalar(select(Service).where(
            Service.organization_id == organization.id,
            Service.name == "API Sentinel Backend",
        ))
        key = "as_live_" + secrets.token_urlsafe(32)
        key_hash = hashlib.sha256(key.encode()).hexdigest()
        if service:
            service.ingest_key_hash = key_hash
            service.environment = "development"
        else:
            service = Service(
                id=new_id(), organization_id=organization.id,
                name="API Sentinel Backend", environment="development",
                ingest_key_hash=key_hash,
            )
            session.add(service)

        env_path = ROOT / ".env"
        existing = env_path.read_text() if env_path.exists() else ""
        env_keys = ("API_SENTINEL_KEY=", "SENTINEL_COLLECTOR_URL=", "SENTINEL_REGION=")
        retained = [line for line in existing.splitlines() if not line.startswith(env_keys)]
        retained.extend([
            f"API_SENTINEL_KEY={key}",
            "SENTINEL_COLLECTOR_URL=http://127.0.0.1:8000/api/telemetry/batch",
            "SENTINEL_REGION=local",
        ])
        temporary = ROOT / ".env.tmp"
        temporary.write_text("\n".join(retained) + "\n")
        os.chmod(temporary, 0o600)
        try:
            await session.commit()
            os.replace(temporary, env_path)
            os.chmod(env_path, 0o600)
        except Exception:
            await session.rollback()
            temporary.unlink(missing_ok=True)
            raise

        print("Connected API Sentinel Backend to the single existing workspace. Key stored in backend/.env (not displayed).")


if __name__ == "__main__":
    asyncio.run(provision())
