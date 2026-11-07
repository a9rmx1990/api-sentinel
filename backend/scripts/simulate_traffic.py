"""Generate explicitly synthetic request telemetry for model-development profiles."""
import argparse
import asyncio
import os
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")


def make_event(index: int, count: int, start: datetime, hours: float, profile: str, rng: random.Random) -> dict:
    progress = index / max(count - 1, 1)
    if profile == "flash" and progress > .9:
        elapsed = hours * .9 + rng.random() * (2 / 60)
    else:
        elapsed = progress * hours + rng.random() * hours / count
    timestamp = start + timedelta(hours=elapsed)
    if profile == "degradation" and progress > .55:
        severity = (progress - .55) / .45
        latency = 35 + 2200 * severity**2 + rng.uniform(0, 25 + 100 * severity)
        status = 503 if severity > .88 and rng.random() < .12 else 200
    elif profile == "cascade" and progress > .7:
        severity = (progress - .7) / .3
        latency = rng.uniform(250, 2500 + severity * 1000)
        status = rng.choices([200, 500, 502, 503], weights=[max(3, 65 - severity * 60), 15, 10, 10])[0]
    else:
        latency = rng.uniform(20, 65)
        status = 200 if rng.random() > .015 else 404
        if profile == "flash" and progress > .9:
            latency *= rng.uniform(1.3, 2.5)
    endpoint = rng.choice(["/api/v1/orders", "/api/v1/products", "/api/v1/health"])
    method = "GET" if endpoint.endswith(("products", "health")) else rng.choice(["GET", "POST"])
    return {
        "event_id": str(uuid4()), "timestamp": timestamp.isoformat(),
        "endpoint": endpoint, "method": method, "status_code": status,
        "latency_ms": round(latency, 2), "request_size": rng.randint(100, 1200),
        "response_size": rng.randint(200, 8000), "region": "synthetic",
    }


async def run(profile: str, count: int, duration_hours: float) -> None:
    key = os.getenv("API_SENTINEL_KEY")
    if not key:
        raise SystemExit("Set API_SENTINEL_KEY or provision self-monitoring before using this simulator.")
    collector = os.getenv("SENTINEL_COLLECTOR_URL", "http://127.0.0.1:8000/api/telemetry/batch")
    rng = random.Random(31)
    start = datetime.now(timezone.utc) - timedelta(hours=duration_hours)
    sent = 0
    async with httpx.AsyncClient(timeout=15) as client:
        for offset in range(0, count, 100):
            events = [make_event(index, count, start, duration_hours, profile, rng)
                      for index in range(offset, min(offset + 100, count))]
            response = await client.post(collector, headers={"X-Sentinel-Key": key}, json={"events": events})
            response.raise_for_status()
            sent += response.json().get("accepted", 0)
    print(f"Sent {sent} synthetic {profile} events to the configured service; region=synthetic.")
    print("These events are stored with normal telemetry and can affect incidents and model labels.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=("normal", "degradation", "cascade", "flash"), required=True)
    parser.add_argument("--events", type=int, default=10_000)
    parser.add_argument("--duration-hours", type=float, default=24)
    args = parser.parse_args()
    if args.events < 100 or args.events > 100_000 or not 1 <= args.duration_hours <= 168:
        parser.error("events must be 100..100000 and duration-hours must be 1..168")
    asyncio.run(run(args.profile, args.events, args.duration_hours))


if __name__ == "__main__":
    main()
