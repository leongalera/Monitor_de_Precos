import logging

from app.config import CHECK_INTERVAL_HOURS, LOG_LEVEL
from app.db import init_db
from app.scheduler import build_scheduler

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("monitor")


def main() -> None:
    init_db()
    scheduler = build_scheduler()
    logger.info("Monitor de preços iniciado. Verificações a cada %s hora(s).", CHECK_INTERVAL_HOURS)

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Monitor de preços encerrado.")


if __name__ == "__main__":
    main()
