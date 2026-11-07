import os

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+asyncpg://sentinel:sentinel@localhost:5432/sentinel",
)
engine = create_async_engine(DATABASE_URL, pool_pre_ping=True)
SessionFactory = async_sessionmaker(engine, expire_on_commit=False)


async def set_tenant(session, organization_id: str) -> None:
    await session.execute(
        text("SELECT set_config('app.organization_id', :organization_id, true)"),
        {"organization_id": organization_id},
    )


async def set_ingest_key(session, key_hash: str) -> None:
    await session.execute(
        text("SELECT set_config('app.ingest_key_hash', :key_hash, true)"),
        {"key_hash": key_hash},
    )
