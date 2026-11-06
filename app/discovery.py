import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from concurrent.futures import TimeoutError as FuturesTimeout
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlsplit

import requests

from app.ai import ai_enabled, suggest_selector
from app.config import SERPER_API_KEY
from app.scraper import extract_page_name, extract_price_with_source, fetch_html

logger = logging.getLogger(__name__)

SERPER_SEARCH_URL = "https://google.serper.dev/search"
SEARCH_RESULTS = 20
MAX_PAGES_TO_READ = 15
READ_WORKERS = 8
READ_DEADLINE_SECONDS = 30


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
    price: float


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


def _store_name(link: str) -> str:
    host = urlsplit(link).hostname or ""
    return host[4:] if host.startswith("www.") else host


def _read_offer(title: str, link: str) -> Optional[Offer]:
    """Só aceita páginas com preço em dados estruturados (JSON-LD): é o que dá para monitorar sem IA."""
    html = fetch_html(link)
    if html is None:
        return None
    price, source = extract_price_with_source(html)
    if price is None or source != "json-ld":
        return None
    return Offer(title=extract_page_name(html) or title, store=_store_name(link), link=link, price=price)


def search_offers(query: str) -> list[Offer]:
    """Busca páginas de lojas (Serper) e devolve só as que tiveram o preço lido, do menor para o maior."""
    if not SERPER_API_KEY:
        raise SearchError("Busca desativada: defina SERPER_API_KEY.")

    try:
        response = requests.post(
            SERPER_SEARCH_URL,
            headers={"X-API-KEY": SERPER_API_KEY, "Content-Type": "application/json"},
            json={"q": f"{query} comprar", "gl": "br", "hl": "pt-br", "num": SEARCH_RESULTS},
            timeout=15,
        )
        response.raise_for_status()
        data = response.json()
    except (requests.RequestException, ValueError) as exc:
        logger.error("Falha na busca de ofertas: %s", type(exc).__name__)
        raise SearchError("Falha ao consultar a API de busca.") from exc

    pages, seen = [], set()
    for item in data.get("organic") or []:
        title, link = item.get("title"), item.get("link")
        host = urlsplit(link).hostname or "" if isinstance(link, str) else ""
        if not title or not link or link in seen or not link.startswith(("http://", "https://")) or "google." in host:
            continue
        seen.add(link)
        pages.append((title, link))
    pages = pages[:MAX_PAGES_TO_READ]

    offers = []
    pool = ThreadPoolExecutor(max_workers=READ_WORKERS)
    try:
        futures = [pool.submit(_read_offer, title, link) for title, link in pages]
        try:
            for future in as_completed(futures, timeout=READ_DEADLINE_SECONDS):
                try:
                    offer = future.result()
                except Exception:
                    logger.exception("Erro ao ler página de oferta")
                    continue
                if offer:
                    offers.append(offer)
        except FuturesTimeout:
            logger.warning("Tempo esgotado lendo ofertas; mostrando as já lidas.")
    finally:
        pool.shutdown(wait=False, cancel_futures=True)

    offers.sort(key=lambda offer: offer.price)
    return offers
