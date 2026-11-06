import ipaddress
import json
import logging
import random
import re
import socket
from typing import Any, Iterator, Optional
from urllib.parse import urljoin, urlsplit

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

# Usados quando o produto não tem seletor próprio nem JSON-LD com preço.
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
ARIA_PRICE_PATTERN = re.compile(r"(\d[\d.]*)\s+reais?(?:\s+com\s+(\d{1,2})\s+centavos?)?", re.IGNORECASE)
SPACE_BEFORE_SEPARATOR = re.compile(r"\s+([,.])(?=\d)")
SPLIT_CENTS = re.compile(r"^(R\$\s*\d{1,3}(?:\.\d{3})*)\s+(\d{2})$")
MAX_REDIRECTS = 5
MAX_RESPONSE_BYTES = 8 * 1024 * 1024


def _random_headers() -> dict:
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
    }


def is_safe_url(url: str) -> bool:
    """Aceita só http(s) cujo host resolve exclusivamente para IPs públicos (evita SSRF)."""
    try:
        parts = urlsplit(url)
        host = parts.hostname
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError:
        return False

    if parts.scheme not in ("http", "https") or not host:
        return False

    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError):
        return False

    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if ip.version == 6 and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        if not ip.is_global:
            return False
    return bool(infos)


def _decode(raw: bytes, response: requests.Response) -> str:
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode(response.encoding or "latin-1", errors="replace")


def fetch_page(url: str) -> tuple[Optional[str], Optional[str]]:
    """Devolve (html, None) ou (None, motivo curto da falha)."""
    session = requests.Session()
    headers = _random_headers()
    current = url

    try:
        for _ in range(MAX_REDIRECTS + 1):
            if not is_safe_url(current):
                logger.error("URL bloqueada ou inválida: %s", current)
                return None, "URL não permitida"

            response = session.get(
                current,
                headers=headers,
                timeout=REQUEST_TIMEOUT_SECONDS,
                allow_redirects=False,
                stream=True,
            )
            try:
                if response.is_redirect:
                    current = urljoin(current, response.headers["Location"])
                    continue

                response.raise_for_status()
                chunks, size = [], 0
                for chunk in response.iter_content(65536):
                    size += len(chunk)
                    if size > MAX_RESPONSE_BYTES:
                        break
                    chunks.append(chunk)
                return _decode(b"".join(chunks), response), None
            finally:
                response.close()

        logger.error("Redirecionamentos demais em %s", url)
        return None, "redirecionamentos demais"
    except requests.HTTPError as exc:
        logger.error("Falha ao buscar %s: %s", url, exc)
        status = exc.response.status_code if exc.response is not None else "?"
        return None, f"HTTP {status}"
    except requests.Timeout:
        logger.error("Tempo esgotado ao buscar %s", url)
        return None, "tempo esgotado"
    except requests.ConnectionError:
        logger.error("Falha de conexão ao buscar %s", url)
        return None, "falha de conexão"
    except requests.RequestException as exc:
        logger.error("Falha ao buscar %s: %s", url, exc)
        return None, "erro de rede"
    finally:
        session.close()


def fetch_html(url: str) -> Optional[str]:
    return fetch_page(url)[0]


def fetch_html_with_playwright(url: str) -> Optional[str]:
    """Ponto de extensão para páginas que dependem de renderização JS.

    Ativado por produto (render_js=True). Requer, fora do requirements.txt base:
    `pip install playwright && playwright install --with-deps chromium`.
    """
    if not is_safe_url(url):
        logger.error("URL bloqueada ou inválida: %s", url)
        return None

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
    return f"R$ {format_number_br(value)}"


def format_number_br(value: float) -> str:
    """Converte 3499.0 em '3.499,00'."""
    return f"{value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _to_price(value: Any) -> Optional[float]:
    """Preço vindo de JSON-LD: números e strings com ponto decimal (schema.org)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if value > 0 else None
    if isinstance(value, str):
        try:
            number = float(value.strip())
        except ValueError:
            return clean_price_string(value)
        return number if number > 0 else None
    return None


def _json_ld_nodes(data: Any) -> Iterator[dict]:
    if isinstance(data, list):
        for item in data:
            yield from _json_ld_nodes(item)
    elif isinstance(data, dict):
        yield data
        yield from _json_ld_nodes(data.get("@graph"))


def _is_product(node: dict) -> bool:
    node_type = node.get("@type")
    types = node_type if isinstance(node_type, list) else [node_type]
    return "Product" in types


def _offers_price(offers: Any) -> Optional[float]:
    if isinstance(offers, list):
        prices = [p for p in (_offers_price(o) for o in offers) if p is not None]
        return min(prices) if prices else None
    if not isinstance(offers, dict):
        return None

    for key in ("price", "lowPrice"):
        price = _to_price(offers.get(key))
        if price is not None:
            return price

    spec = offers.get("priceSpecification")
    if isinstance(spec, (dict, list)):
        for item in spec if isinstance(spec, list) else [spec]:
            price = _to_price(item.get("price")) if isinstance(item, dict) else None
            if price is not None:
                return price
    return None


def _json_ld_products(soup: BeautifulSoup) -> Iterator[dict]:
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or script.get_text() or "")
        except ValueError:
            continue
        for node in _json_ld_nodes(data):
            if _is_product(node):
                yield node


def _price_from_json_ld(soup: BeautifulSoup) -> Optional[float]:
    for product in _json_ld_products(soup):
        price = _offers_price(product.get("offers"))
        if price is not None:
            return price
    return None


def json_ld_summary(html: str) -> str:
    """Resumo dos dados estruturados da página, para diagnóstico."""
    soup = BeautifulSoup(html, "lxml")
    blocks = soup.find_all("script", type="application/ld+json")
    if not blocks:
        return "nenhum bloco JSON-LD"

    types, invalid = [], 0
    for script in blocks:
        try:
            data = json.loads(script.string or script.get_text() or "")
        except ValueError:
            invalid += 1
            continue
        for node in _json_ld_nodes(data):
            node_type = node.get("@type")
            types.extend(node_type if isinstance(node_type, list) else [node_type])

    has_price = _price_from_json_ld(soup) is not None
    summary = f"{len(blocks)} bloco(s), tipos: {', '.join(str(t) for t in dict.fromkeys(types) if t) or '-'}"
    if invalid:
        summary += f", {invalid} inválido(s)"
    return summary + f"; preço em Product: {'sim' if has_price else 'não'}"


def extract_page_name(html: str) -> Optional[str]:
    soup = BeautifulSoup(html, "lxml")

    for product in _json_ld_products(soup):
        name = product.get("name")
        if isinstance(name, str) and name.strip():
            return name.strip()

    og_title = soup.find("meta", property="og:title")
    if og_title and og_title.get("content", "").strip():
        return og_title["content"].strip()

    if soup.title and soup.title.get_text(strip=True):
        return soup.title.get_text(strip=True)
    return None


def element_price_text(element) -> str:
    """Texto do preço de um elemento, tratando preços partidos em vários spans.

    Ex.: <span>R$</span><span>9.912</span><span>80</span> -> "R$ 9.912,80" (sem isso viraria "R$9.91280").
    Usa o aria-label "9912 reais com 80 centavos" quando existir.
    """
    for node in [element, *element.find_all(attrs={"aria-label": True})]:
        match = ARIA_PRICE_PATTERN.search(node.get("aria-label", ""))
        if match:
            return f"R$ {match.group(1).replace('.', '')},{(match.group(2) or '0').zfill(2)}"

    text = element.get_text(" ", strip=True)
    text = SPACE_BEFORE_SEPARATOR.sub(r"\1", text)
    return SPLIT_CENTS.sub(r"\1,\2", text)


def price_from_selector(soup: BeautifulSoup, css_selector: str) -> Optional[float]:
    try:
        element = soup.select_one(css_selector)
    except Exception as exc:
        logger.warning("Seletor CSS inválido %r: %s", css_selector, exc)
        return None
    if element is None:
        return None
    return clean_price_string(element.get("content") or element_price_text(element))


def extract_price_with_source(html: str, selector: Optional[str] = None) -> tuple[Optional[float], Optional[str]]:
    """Ordem: seletor do produto, JSON-LD (schema.org), seletores genéricos."""
    soup = BeautifulSoup(html, "lxml")

    if selector:
        price = price_from_selector(soup, selector)
        if price is not None:
            return price, "seletor"

    price = _price_from_json_ld(soup)
    if price is not None:
        return price, "json-ld"

    for css_selector in FALLBACK_SELECTORS:
        price = price_from_selector(soup, css_selector)
        if price is not None:
            return price, "css"

    return None, None


def extract_price(html: str, selector: Optional[str] = None) -> Optional[float]:
    return extract_price_with_source(html, selector)[0]


def is_valid_selector(selector: str) -> bool:
    try:
        BeautifulSoup("", "lxml").select_one(selector)
    except Exception:
        return False
    return True


def get_product_price(url: str, selector: Optional[str] = None, render_js: bool = False) -> Optional[float]:
    html = fetch_html_with_playwright(url) if render_js else fetch_html(url)
    if html is None:
        return None
    return extract_price(html, selector)
