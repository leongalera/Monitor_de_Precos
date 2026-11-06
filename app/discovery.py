import logging
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlsplit

import requests

from app.ai import ai_enabled, suggest_selector
from app.config import SERPER_API_KEY
from app.scraper import clean_price_string, extract_page_name, extract_price_with_source, fetch_html

logger = logging.getLogger(__name__)

SERPER_SHOPPING_URL = "https://google.serper.dev/shopping"
SEARCH_RESULTS = 20


class InspectError(Exception):
    pass


class SearchError(Exception):
    pass


@dataclass
class ProductInfo:
    url: str
    name: Optional[str]
    price: Optional[float]
    selector: Optional[str]
    source: Optional[str]  # "json-ld" | "css" | "ia"


@dataclass
class Offer:
    title: str
    store: str
    link: str
    price: Optional[float]
    direct: bool  # False quando o link aponta para o próprio Google Shopping


def inspect_url(url: str) -> ProductInfo:
    """Lê a página e descobre nome, preço e (se preciso) um seletor CSS para o preço."""
    html = fetch_html(url)
    if html is None:
        raise InspectError("Não foi possível acessar a página (URL inválida, bloqueio da loja ou site fora do ar).")

    name = extract_page_name(html)
    price, source = extract_price_with_source(html)
    selector = None

    # O seletor genérico ("css") é pouco confiável (pode pegar produto relacionado): a IA tem a palavra final.
    if (price is None or source == "css") and ai_enabled():
        selector = suggest_selector(html)
        if selector:
            price, _ = extract_price_with_source(html, selector)
            source = "ia"

    return ProductInfo(url=url, name=name, price=price, selector=selector, source=source)


def search_offers(query: str) -> list[Offer]:
    if not SERPER_API_KEY:
        raise SearchError("Busca desativada: defina SERPER_API_KEY.")

    try:
        response = requests.post(
            SERPER_SHOPPING_URL,
            headers={"X-API-KEY": SERPER_API_KEY, "Content-Type": "application/json"},
            json={"q": query, "gl": "br", "hl": "pt-br", "num": SEARCH_RESULTS},
            timeout=15,
        )
        response.raise_for_status()
        data = response.json()
    except (requests.RequestException, ValueError) as exc:
        logger.error("Falha na busca de ofertas: %s", type(exc).__name__)
        raise SearchError("Falha ao consultar a API de busca.") from exc

    offers = []
    for item in data.get("shopping") or []:
        title, link = item.get("title"), item.get("link")
        if not title or not link:
            continue
        host = urlsplit(link).hostname or ""
        offers.append(
            Offer(
                title=title,
                store=item.get("source") or host,
                link=link,
                price=clean_price_string(str(item.get("price") or "")),
                direct="google." not in host,
            )
        )

    offers.sort(key=lambda offer: (offer.price is None, offer.price or 0))
    return offers
