import logging
import random
import re
from typing import Optional

import requests
from bs4 import BeautifulSoup

from app.config import REQUEST_TIMEOUT_SECONDS

logger = logging.getLogger(__name__)

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1",
]

# Seletores usados quando o produto não define um price_selector específico.
FALLBACK_SELECTORS = [
    "meta[itemprop='price']",
    "[itemprop='price']",
    "meta[property='product:price:amount']",
    "meta[property='og:price:amount']",
    ".price",
    ".preco",
    ".product-price",
]

PRICE_NUMBER_PATTERN = re.compile(r"\d[\d.,]*")


def _random_headers() -> dict:
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
    }


def fetch_html(url: str) -> Optional[str]:
    try:
        response = requests.get(url, headers=_random_headers(), timeout=REQUEST_TIMEOUT_SECONDS)
        response.raise_for_status()
        return response.text
    except requests.RequestException as exc:
        logger.error("Falha ao buscar %s: %s", url, exc)
        return None


def fetch_html_with_playwright(url: str) -> Optional[str]:
    """Ponto de extensão para páginas que dependem de renderização JS.

    Ativado por produto (render_js=True). Requer, fora do requirements.txt base:
    `pip install playwright && playwright install --with-deps chromium`.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        logger.error("Playwright não instalado; não é possível renderizar %s", url)
        return None

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page(user_agent=random.choice(USER_AGENTS))
                page.goto(url, timeout=int(REQUEST_TIMEOUT_SECONDS * 1000), wait_until="networkidle")
                return page.content()
            finally:
                browser.close()
    except Exception as exc:
        logger.error("Falha ao buscar %s via Playwright: %s", url, exc)
        return None


def clean_price_string(raw_price: str) -> Optional[float]:
    """Converte 'R$ 3.499,00' em 3499.0. Aceita também '3499.00' e '3.499'."""
    if not raw_price:
        return None

    match = PRICE_NUMBER_PATTERN.search(raw_price)
    if not match:
        return None

    number = match.group(0).rstrip(".,")

    if "," in number:
        number = number.replace(".", "").replace(",", ".")
    elif number.count(".") > 1:
        number = number.replace(".", "")
    elif "." in number and len(number.rsplit(".", 1)[1]) == 3:
        number = number.replace(".", "")

    try:
        return float(number)
    except ValueError:
        return None


def format_price_brl(value: float) -> str:
    """Converte 3499.0 em 'R$ 3.499,00'."""
    formatted = f"{value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"R$ {formatted}"


def extract_price(html: str, selector: Optional[str] = None) -> Optional[float]:
    soup = BeautifulSoup(html, "lxml")

    selectors = ([selector] if selector else []) + FALLBACK_SELECTORS

    for css_selector in selectors:
        element = soup.select_one(css_selector)
        if element is None:
            continue

        raw_value = element.get("content") or element.get_text(strip=True)
        price = clean_price_string(raw_value)
        if price is not None:
            return price

    return None


def get_product_price(url: str, selector: Optional[str] = None, render_js: bool = False) -> Optional[float]:
    html = fetch_html_with_playwright(url) if render_js else fetch_html(url)
    if html is None:
        return None
    return extract_price(html, selector)
