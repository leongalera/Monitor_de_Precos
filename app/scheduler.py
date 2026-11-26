import logging
import threading
import time
from datetime import datetime, timedelta
from urllib.parse import urlsplit

from apscheduler.schedulers.background import BackgroundScheduler

from app import health
from app.config import ALERT_COOLDOWN_HOURS, CHECK_INTERVAL_HOURS, HEALTH_WATCHDOG_MINUTES
from app.db import Product, SessionLocal, utcnow
from app.notifier import build_price_drop_message, send_telegram_message
from app.scraper import format_price_brl, read_product_price

logger = logging.getLogger(__name__)


def _cooldown_elapsed(product: Product) -> bool:
    if product.last_alert_sent_at is None:
        return True
    return utcnow() - product.last_alert_sent_at >= timedelta(hours=ALERT_COOLDOWN_HOURS)


def _display_name(product: Product) -> str:
    if product.group is None:
        return product.name
    host = urlsplit(product.url).hostname or ""
    return f"{product.group.name} ({host[4:] if host.startswith('www.') else host})"


def check_product(session, product: Product) -> None:
    logger.info("Verificando '%s' (%s)", product.name, product.url)

    price, error = read_product_price(product.url, product.price_selector, product.render_js)
    product.last_checked_at = utcnow()
    product.last_error = None if price is not None else (error or "falha ao ler o preço")[:200]

    if price is None:
        logger.warning("Não foi possível extrair o preço de '%s': %s", product.name, product.last_error)
        session.commit()
        return

    product.last_price = price

    if price > product.target_price:
        logger.info(
            "'%s': %s acima do alvo %s",
            product.name,
            format_price_brl(price),
            format_price_brl(product.target_price),
        )
    elif not _cooldown_elapsed(product):
        logger.info("'%s' abaixo do alvo, mas alerta em cooldown.", product.name)
    else:
        message = build_price_drop_message(_display_name(product), price, product.target_price, product.url)
        if send_telegram_message(message):
            product.last_alert_sent_at = utcnow()
            logger.info("Alerta enviado para '%s'", product.name)

    session.commit()


def check_product_by_id(product_id: int) -> None:
    with SessionLocal() as session:
        product = session.get(Product, product_id)
        if product is None or not product.active:
            return
        try:
            check_product(session, product)
        except Exception:
            logger.exception("Erro ao verificar '%s'", product.name)
            session.rollback()


# Mesmo com prazos nas leituras, uma verificação não pode derrubar o job inteiro: se uma passar disso, o job segue.
PRODUCT_CHECK_TIMEOUT_SECONDS = 300


def _mark_error(product_id: int, message: str) -> None:
    try:
        with SessionLocal() as session:
            product = session.get(Product, product_id)
            if product is not None:
                product.last_error = message
                product.last_checked_at = utcnow()
                session.commit()
    except Exception:
        logger.exception("Não foi possível registrar o erro do produto %s", product_id)


def check_all_products() -> None:
    started = time.monotonic()
    with SessionLocal() as session:
        product_ids = [pid for (pid,) in session.query(Product.id).filter(Product.active.is_(True)).order_by(Product.id)]
    logger.info("Verificando %d produto(s) ativo(s)", len(product_ids))
    health.run_started(len(product_ids))

    try:
        for product_id in product_ids:
            product_started = time.monotonic()
            try:
                worker = threading.Thread(target=check_product_by_id, args=(product_id,), daemon=True)
                worker.start()
                worker.join(PRODUCT_CHECK_TIMEOUT_SECONDS)
                if worker.is_alive():
                    logger.error(
                        "Verificação do produto id=%s travada por mais de %ss; seguindo para o próximo.",
                        product_id,
                        PRODUCT_CHECK_TIMEOUT_SECONDS,
                    )
                    _mark_error(product_id, "tempo esgotado (a verificação travou)")
                    continue

                elapsed = time.monotonic() - product_started
                if elapsed > 30:
                    logger.warning("Verificação lenta (%.0fs): produto id=%s", elapsed, product_id)
            finally:
                health.product_done()
    finally:
        elapsed_total = time.monotonic() - started
        health.run_finished(elapsed_total)
        logger.info("Verificação concluída: %d produto(s) em %.0fs", len(product_ids), elapsed_total)


def build_scheduler() -> BackgroundScheduler:
    scheduler = BackgroundScheduler()
    scheduler.add_job(
        check_all_products,
        "interval",
        hours=CHECK_INTERVAL_HOURS,
        id="check_all_products",
        next_run_time=datetime.now(),
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        health.check_and_alert,
        "interval",
        minutes=HEALTH_WATCHDOG_MINUTES,
        id="health_watchdog",
        max_instances=1,
        coalesce=True,
    )
    health.set_scheduler(scheduler)
    return scheduler
