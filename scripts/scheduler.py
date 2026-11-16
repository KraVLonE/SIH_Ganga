"""Run the daily job at a fixed local time, forever. Used by the worker container."""

import os
import subprocess
import sys
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

TZ = ZoneInfo(os.environ.get("GANGA_TZ", "Asia/Kolkata"))
HOUR, MINUTE = (int(x) for x in os.environ.get("GANGA_DAILY_AT", "06:30").split(":"))


def next_run(now: datetime) -> datetime:
    target = now.replace(hour=HOUR, minute=MINUTE, second=0, microsecond=0)
    return target if target > now else target + timedelta(days=1)


def run() -> None:
    print(f"[{datetime.now(TZ):%Y-%m-%d %H:%M}] daily job starting", flush=True)
    result = subprocess.run(["sh", "scripts/daily.sh"], check=False)
    print(
        f"[{datetime.now(TZ):%Y-%m-%d %H:%M}] daily job exit code {result.returncode}", flush=True
    )


if __name__ == "__main__":
    if "--now" in sys.argv:
        run()
    while True:
        wait = (next_run(datetime.now(TZ)) - datetime.now(TZ)).total_seconds()
        time.sleep(max(1.0, wait))
        run()
