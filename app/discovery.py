import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from concurrent.futures import TimeoutError as FuturesTimeout
from collections import Counter
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlsplit

import requests

from app.ai import ai_enabled, suggest_selector
from app.config import SERPER_API_KEY
from app.scraper import extract_page_name, extract_price_with_source, fetch_html, fetch_page

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


@dataclass
class SkippedPage:
    store: str
    reason: str


@dataclass
class SearchOutcome:
    offers: list[Offer]
    skipped: list[SkippedPage] = field(default_factory=list)


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


def _read_offer(title: str, link: str) -> tuple[Optional[Offer], Optional[str]]:
    """Só aceita páginas com preço em dados estruturados (JSON-LD): é o que dá para monitorar sem IA."""
    html, error = fetch_page(link)
    if html is None:
        if error and error.startswith("HTTP"):
            return None, f"acesso recusado pela loja ({error})"
        return None, error or "falha ao acessar"

    price, source = extract_price_with_source(html)
    if price is None:
        return None, "página lida, mas sem preço identificável"
    if source != "json-ld":
        return None, "preço só em seletor genérico (cadastre pelo link para a IA tentar)"
    return Offer(title=extract_page_name(html) or title, store=_store_name(link), link=link, price=price), None


def _log_outcome(query: str, organic_hosts: list[str], outcome: SearchOutcome) -> None:
    reasons = Counter(skipped.reason for skipped in outcome.skipped)
    logger.info(
        "Busca %r: %d resultado(s) orgânico(s) [%s]; %d oferta(s) legível(is); descartes: %s",
        query,
        len(organic_hosts),
        ", ".join(organic_hosts),
        len(outcome.offers),
        dict(reasons) or "nenhum",
    )
    for skipped in outcome.skipped:
        logger.info("  descartada %s: %s", skipped.store, skipped.reason)


def search_offers(query: str) -> SearchOutcome:
    """Busca páginas de lojas (Serper) e devolve as ofertas com preço lido (menor primeiro) e os descartes."""
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

    candidates, seen = [], set()
    for item in data.get("organic") or []:
        title, link = item.get("title"), item.get("link")
        host = urlsplit(link).hostname or "" if isinstance(link, str) else ""
        if not title or not link or link in seen or not link.startswith(("http://", "https://")) or "google." in host:
            continue
        seen.add(link)
        candidates.append((title, link))

    pages, over_limit = candidates[:MAX_PAGES_TO_READ], candidates[MAX_PAGES_TO_READ:]
    offers = []
    skipped = [SkippedPage(_store_name(link), f"não lida (limite de {MAX_PAGES_TO_READ} páginas)") for _, link in over_limit]

    pool = ThreadPoolExecutor(max_workers=READ_WORKERS)
    try:
        futures = {pool.submit(_read_offer, title, link): link for title, link in pages}
        try:
            for future in as_completed(futures, timeout=READ_DEADLINE_SECONDS):
                try:
                    offer, reason = future.result()
                except Exception:
                    logger.exception("Erro ao ler página de oferta")
                    skipped.append(SkippedPage(_store_name(futures[future]), "erro interno ao ler a página"))
                    continue
                if offer:
                    offers.append(offer)
                else:
                    skipped.append(SkippedPage(_store_name(futures[future]), reason or "descartada"))
        except FuturesTimeout:
            logger.warning("Tempo esgotado lendo ofertas; mostrando as já lidas.")
            for future, link in futures.items():
                if not future.done():
                    skipped.append(SkippedPage(_store_name(link), "tempo esgotado"))
    finally:
        pool.shutdown(wait=False, cancel_futures=True)

    offers.sort(key=lambda offer: offer.price)
    outcome = SearchOutcome(offers=offers, skipped=skipped)
    _log_outcome(query, [_store_name(link) for _, link in candidates], outcome)
    return outcome
