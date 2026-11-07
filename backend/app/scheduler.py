"""Single scheduler process; Redis locks each time slot before enqueueing jobs."""
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import redis
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env")
from app.tasks import analyze_all_organizations_task, dispatch_notifications_task, train_models_task


def run() -> None:
    client = redis.Redis.from_url(os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0"), decode_responses=True)
    client.ping()
    print("API Sentinel scheduler started: analysis every minute, model evaluation daily.")
    while True:
        client.set("sentinel:scheduler:heartbeat", datetime.now(timezone.utc).isoformat(), ex=90)
        minute_slot = int(time.time() // 60)
        if client.set(f"sentinel:schedule:analysis:{minute_slot}", "1", nx=True, ex=180):
            analyze_all_organizations_task.send()
            dispatch_notifications_task.send()
            if minute_slot % (24 * 60) == 0:
                train_models_task.send()
        time.sleep(1)


if __name__ == "__main__":
    run()
