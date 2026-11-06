import logging
from datetime import datetime, timedelta
from urllib.parse import urlsplit

from apscheduler.schedulers.background import BackgroundScheduler

from app.config import ALERT_COOLDOWN_HOURS, CHECK_INTERVAL_HOURS
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


def check_all_products() -> None:
    session = SessionLocal()
    try:
        products = session.query(Product).filter(Product.active.is_(True)).all()
        logger.info("Verificando %d produto(s) ativo(s)", len(products))
        for product in products:
            try:
                check_product(session, product)
            except Exception:
                logger.exception("Erro ao verificar '%s'", product.name)
                session.rollback()
    finally:
        session.close()


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
    return scheduler
