import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from concurrent.futures import TimeoutError as FuturesTimeout
from collections import Counter
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlsplit

import requests
from bs4 import BeautifulSoup

from app.ai import ai_enabled, price_candidates, suggest_selector
from app.config import SERPER_API_KEY
from app.scraper import extract_page_name, extract_price_with_source, fetch_html, fetch_page, json_ld_summary

logger = logging.getLogger(__name__)

SERPER_SEARCH_URL = "https://google.serper.dev/search"
SEARCH_RESULTS = 20
MAX_PAGES_TO_READ = 15
READ_WORKERS = 8
READ_DEADLINE_SECONDS = 30
MAX_EVIDENCE_CANDIDATES = 6
# Redes sociais, vídeo e reclamações: aparecem nos resultados mas não vendem produto.
NON_STORE_DOMAINS = (
    "facebook.com", "instagram.com", "youtube.com", "tiktok.com", "pinterest.com", "x.com", "twitter.com",
    "reddit.com", "linkedin.com", "reclameaqui.com.br", "wikipedia.org",
)


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
    evidence: list[str] = field(default_factory=list)  # preenchido quando o preço não foi lido


@dataclass
class Offer:
    title: str
    store: str
    link: str
    price: float
    selector: Optional[str] = None  # preenchido quando o preço veio de um seletor sugerido pela IA


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
        selector = suggest_selector(html, url)
        if selector:
            price, _ = extract_price_with_source(html, selector)
            source = "ia"

    evidence = [] if price is not None else _evidence(html)
    return ProductInfo(url=url, name=name, price=price, selector=selector, source=source, evidence=evidence)


def _evidence(html: str) -> list[str]:
    """O que existe na página que poderia ser o preço, para diagnosticar sem precisar do HTML."""
    soup = BeautifulSoup(html, "lxml")
    lines = [
        f"Dados estruturados: {json_ld_summary(html)}",
        f"Tags de preço (itemprop/meta): {len(soup.select('[itemprop=price], meta[property*=price]'))}",
    ]
    candidates = price_candidates(html)[:MAX_EVIDENCE_CANDIDATES]
    if candidates:
        lines.append("Elementos com valor em R$:")
        lines += [f"  {c['selector']} → {c['text']}" for c in candidates]
    else:
        lines.append("Nenhum elemento com valor em R$ no HTML recebido (o preço pode ser carregado por JavaScript).")
    return lines


def _is_non_store(host: str) -> bool:
    return any(host == domain or host.endswith("." + domain) for domain in NON_STORE_DOMAINS)


def _store_name(link: str) -> str:
    host = urlsplit(link).hostname or ""
    return host[4:] if host.startswith("www.") else host


CHALLENGE_MARKERS = (
    "robot check", "captcha", "just a moment", "access denied", "are you a human", "enable javascript",
    "digite os caracteres", "verificação de segurança", "acesse sua conta", "para continuar, acesse",
)


def _describe_unreadable_page(html: str) -> str:
    """Explica por que uma página que respondeu 200 não trouxe preço (ex.: captcha/login em vez do produto)."""
    soup = BeautifulSoup(html, "lxml")
    title = soup.title.get_text(strip=True)[:60] if soup.title else ""
    sample = f"{title} {soup.get_text(' ', strip=True)[:1500]}".lower()

    if any(marker in sample for marker in CHALLENGE_MARKERS):
        return f"página de verificação anti-bot (título: '{title}')" if title else "página de verificação anti-bot"
    if title:
        return f"página lida, mas sem preço identificável (título: '{title}')"
    return "página lida, mas sem preço identificável"


def _read_offer(title: str, link: str) -> tuple[Optional[Offer], Optional[str]]:
    """Aceita preço de JSON-LD ou de um seletor que a IA escolheu e que foi conferido na página."""
    html, error = fetch_page(link)
    if html is None:
        if error and error.startswith("HTTP"):
            return None, f"acesso recusado pela loja ({error})"
        return None, error or "falha ao acessar"

    price, source = extract_price_with_source(html)
    selector = None

    if source != "json-ld" and ai_enabled():
        selector = suggest_selector(html, link)
        if selector:
            price, _ = extract_price_with_source(html, selector)
            source = "ia"

    if price is None:
        return None, _describe_unreadable_page(html)
    if source not in ("json-ld", "ia"):
        return None, "preço só em seletor genérico (cadastre pelo link para a IA tentar)"

    offer = Offer(title=extract_page_name(html) or title, store=_store_name(link), link=link, price=price, selector=selector)
    return offer, None


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
        if not title or not link or link in seen or not link.startswith(("http://", "https://")) or "google." in host or _is_non_store(host):
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
