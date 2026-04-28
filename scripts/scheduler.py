#!/usr/bin/env python3
"""
Scheduler for periodic execution of coverage calculation.
Reads the cron schedule from configuration and launches coverage_calculator.
"""
import logging
import subprocess
import sys
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from meshcoverage.config import settings

logging.basicConfig(
    level=getattr(logging, settings.log_level),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
log = logging.getLogger("meshcoverage.scheduler")


def run_full_computation():
    """Executes coverage calculation for all nodes."""
    log.info("Starting scheduled coverage calculation...")
    result = subprocess.run(
        [sys.executable, "-m", "meshcoverage.processing.coverage_calculator", "--all"],
        cwd=str(project_root),
        capture_output=True,
        text=True
    )
    if result.returncode == 0:
        log.info("Calculation completed successfully")
    else:
        log.error(f"Calculation failed (exit {result.returncode}):\n{result.stderr}")


def main():
    scheduler = BlockingScheduler()

    # Parse cron schedule from config (e.g. "0 3 * * *")
    cron_parts = settings.compute_schedule.split()
    if len(cron_parts) == 5:
        minute, hour, day, month, day_of_week = cron_parts
        trigger = CronTrigger(
            minute=minute, hour=hour,
            day=day, month=month, day_of_week=day_of_week
        )
    else:
        log.warning(f"Invalid schedule '{settings.compute_schedule}', using default 3:00")
        trigger = CronTrigger(hour=3, minute=0)

    scheduler.add_job(run_full_computation, trigger, id="coverage_compute")
    log.info(f"Scheduler started. Schedule: {settings.compute_schedule}")

    # Run immediately on first start if requested
    if "--run-now" in sys.argv:
        log.info("--run-now specified, running immediately...")
        run_full_computation()

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("Scheduler stopped.")


if __name__ == "__main__":
    main()
