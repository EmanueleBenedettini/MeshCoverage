#!/usr/bin/env python3
"""
Scheduler per l'esecuzione periodica del calcolo di copertura.
Legge il cron schedule dalla configurazione e lancia coverage_calculator.
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
    """Esegue il calcolo di copertura per tutti i nodi."""
    log.info("Avvio calcolo copertura schedulato...")
    result = subprocess.run(
        [sys.executable, "-m", "meshcoverage.processing.coverage_calculator", "--all"],
        cwd=str(project_root),
        capture_output=True,
        text=True
    )
    if result.returncode == 0:
        log.info("Calcolo completato con successo")
    else:
        log.error(f"Calcolo fallito (exit {result.returncode}):\n{result.stderr}")


def main():
    scheduler = BlockingScheduler()

    # Parsing del cron schedule dalla config (es. "0 3 * * *")
    cron_parts = settings.compute_schedule.split()
    if len(cron_parts) == 5:
        minute, hour, day, month, day_of_week = cron_parts
        trigger = CronTrigger(
            minute=minute, hour=hour,
            day=day, month=month, day_of_week=day_of_week
        )
    else:
        log.warning(f"Schedule non valido '{settings.compute_schedule}', uso default 3:00")
        trigger = CronTrigger(hour=3, minute=0)

    scheduler.add_job(run_full_computation, trigger, id="coverage_compute")
    log.info(f"Scheduler avviato. Schedule: {settings.compute_schedule}")

    # Esegui subito al primo avvio se richiesto
    if "--run-now" in sys.argv:
        log.info("--run-now specificato, esecuzione immediata...")
        run_full_computation()

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("Scheduler fermato.")


if __name__ == "__main__":
    main()
