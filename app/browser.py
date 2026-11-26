"""Renderização com Chromium (Playwright).

Roda em processo separado (ver scraper.render_page): se o driver do Playwright travar ou cair,
só este processo é afetado e o servidor o encerra à força por tempo.
Uso: python -m app.browser <url>  -> imprime {"html": ..., "error": ...} em JSON.
"""
import json
import logging
import random
import sys
from typing import Optional

from app.scraper import USER_AGENTS, is_safe_url

logger = logging.getLogger(__name__)

BROWSER_ARGS = ["--disable-dev-shm-usage"]
BROWSER_BLOCKED_RESOURCES = {"image", "media", "font"}
BROWSER_NAVIGATION_TIMEOUT_MS = 30000
BROWSER_IDLE_TIMEOUT_MS = 10000
BROWSER_SETTLE_MS = 1500


def _route_request(route) -> None:
    request = route.request
    if request.resource_type in BROWSER_BLOCKED_RESOURCES:
        route.abort()
    elif request.url.startswith(("data:", "blob:")) or is_safe_url(request.url):
        route.continue_()
    else:
        route.abort()


def render_in_process(url: str) -> tuple[Optional[str], Optional[str]]:
    """Abre a página e devolve o HTML já montado pelo JavaScript: (html, None) ou (None, motivo).

    Toda requisição do navegador passa pela checagem anti-SSRF, inclusive redirecionamentos
    e chamadas da própria página.
    """
    from playwright.sync_api import TimeoutError as PlaywrightTimeout
    from playwright.sync_api import sync_playwright

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, args=BROWSER_ARGS)
            try:
                context = browser.new_context(
                    user_agent=random.choice(USER_AGENTS), locale="pt-BR", viewport={"width": 1366, "height": 900}
                )
                page = context.new_page()
                page.route("**/*", _route_request)

                response = page.goto(url, timeout=BROWSER_NAVIGATION_TIMEOUT_MS, wait_until="domcontentloaded")
                if response is not None and response.status >= 400:
                    return None, f"HTTP {response.status}"

                try:
                    page.wait_for_load_state("networkidle", timeout=BROWSER_IDLE_TIMEOUT_MS)
                except PlaywrightTimeout:
                    pass  # sites com polling nunca ficam ociosos: usa o que já carregou
                page.wait_for_timeout(BROWSER_SETTLE_MS)
                return page.content(), None
            finally:
                browser.close()
    except PlaywrightTimeout:
        return None, "tempo esgotado"
    except Exception as exc:
        logger.error("Falha ao renderizar %s: %s", url, exc)
        return None, f"falha ao renderizar ({type(exc).__name__})"


def main(argv: list[str]) -> int:
    html, error = render_in_process(argv[1])
    sys.stdout.write(json.dumps({"html": html, "error": error}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
