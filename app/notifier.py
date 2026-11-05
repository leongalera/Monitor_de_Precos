import html
import logging

import requests

from app.config import TELEGRAM_CHAT_ID, TELEGRAM_TOKEN
from app.scraper import format_price_brl

logger = logging.getLogger(__name__)

TELEGRAM_API_URL = "https://api.telegram.org/bot{token}/sendMessage"


def send_telegram_message(text: str) -> bool:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("TELEGRAM_TOKEN/TELEGRAM_CHAT_ID não configurados; alerta não enviado.")
        return False

    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
    }

    try:
        response = requests.post(TELEGRAM_API_URL.format(token=TELEGRAM_TOKEN), json=payload, timeout=10)
        response.raise_for_status()
        return True
    except requests.RequestException as exc:
        # Não loga a exceção completa: a URL da requisição contém o token do bot.
        logger.error("Falha ao enviar mensagem no Telegram: %s", type(exc).__name__)
        return False


def build_price_drop_message(name: str, current_price: float, target_price: float, url: str) -> str:
    return (
        "🔔 <b>Queda de preço detectada!</b>\n\n"
        f"📦 <b>Produto:</b> {html.escape(name)}\n"
        f"💰 <b>Preço atual:</b> {format_price_brl(current_price)}\n"
        f"🎯 <b>Preço alvo:</b> {format_price_brl(target_price)}\n"
        f"🔗 {html.escape(url)}"
    )
