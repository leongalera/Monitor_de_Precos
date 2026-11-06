import logging
import secrets
from contextlib import asynccontextmanager
from datetime import timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode, urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, BackgroundTasks, Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates

from app.ai import ai_enabled, filter_matching_offers
from app.config import ADMIN_PASSWORD, ADMIN_USER, SERPER_API_KEY
from app.db import Product, SessionLocal, init_db
from app.discovery import InspectError, SearchError, inspect_url, search_offers
from app.scheduler import build_scheduler, check_product_by_id
from app.scraper import clean_price_string, format_number_br, format_price_brl, is_safe_url, is_valid_selector

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent
SUGGESTED_TARGET_FACTOR = 0.95
MAX_OFFERS_PER_REQUEST = 20

try:
    LOCAL_TZ = ZoneInfo("America/Sao_Paulo")
except ZoneInfoNotFoundError:
    LOCAL_TZ = timezone(timedelta(hours=-3))


def local_time(value) -> str:
    if value is None:
        return "-"
    return value.replace(tzinfo=timezone.utc).astimezone(LOCAL_TZ).strftime("%d/%m %H:%M")


templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
templates.env.filters["brl"] = format_price_brl
templates.env.filters["localtime"] = local_time

security = HTTPBasic(auto_error=False)


def require_auth(credentials: HTTPBasicCredentials | None = Depends(security)) -> None:
    if not ADMIN_PASSWORD:
        raise HTTPException(status_code=503, detail="Interface web desativada: defina ADMIN_PASSWORD.")

    valid = credentials is not None
    if valid:
        user_ok = secrets.compare_digest(credentials.username.encode(), ADMIN_USER.encode())
        pass_ok = secrets.compare_digest(credentials.password.encode(), ADMIN_PASSWORD.encode())
        valid = user_ok and pass_ok
    if not valid:
        raise HTTPException(
            status_code=401,
            detail="Credenciais inválidas.",
            headers={"WWW-Authenticate": 'Basic realm="Monitor de Precos"'},
        )


def same_origin_only(request: Request) -> None:
    """Bloqueia POSTs vindos de outros sites (Basic Auth é reenviado automaticamente pelo navegador)."""
    fetch_site = request.headers.get("sec-fetch-site")
    if fetch_site is not None:
        if fetch_site not in ("same-origin", "none"):
            raise HTTPException(status_code=403, detail="Origem inválida.")
        return

    origin = request.headers.get("origin")
    if origin and urlsplit(origin).netloc != request.headers.get("host"):
        raise HTTPException(status_code=403, detail="Origem inválida.")


router = APIRouter(dependencies=[Depends(require_auth)])
post_guard = [Depends(same_origin_only)]


def redirect(message: str = "", kind: str = "ok") -> RedirectResponse:
    query = urlencode({"msg": message[:300], "kind": kind}) if message else ""
    return RedirectResponse(f"/?{query}" if query else "/", status_code=303)


def _product_view(product: Product) -> dict:
    last, target = product.last_price, product.target_price
    return {
        "id": product.id,
        "name": product.name,
        "url": product.url,
        "host": urlsplit(product.url).hostname or "",
        "active": product.active,
        "target": target,
        "target_input": format_number_br(target),
        "last_price": last,
        "below_target": last is not None and last <= target,
        "checked_at": product.last_checked_at,
    }


def _suggested_target(price: float | None) -> str:
    return format_number_br(round(price * SUGGESTED_TARGET_FACTOR, 2)) if price else ""


@router.get("/", response_class=HTMLResponse)
def index(request: Request, msg: str = "", kind: str = "ok"):
    with SessionLocal() as session:
        products = session.query(Product).order_by(Product.active.desc(), Product.name).all()
        views = [_product_view(p) for p in products]

    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "products": views,
            "msg": msg,
            "kind": "err" if kind == "err" else "ok",
            "search_enabled": bool(SERPER_API_KEY),
            "ai_enabled": ai_enabled(),
        },
    )


@router.post("/products/inspect", response_class=HTMLResponse, dependencies=post_guard)
def inspect_product(request: Request, url: str = Form(...)):
    url = url.strip()
    try:
        info = inspect_url(url)
    except InspectError as exc:
        return redirect(str(exc), "err")

    return templates.TemplateResponse(
        request,
        "preview.html",
        {"info": info, "suggested": _suggested_target(info.price)},
    )


@router.post("/products", dependencies=post_guard)
def create_product(
    background: BackgroundTasks,
    name: str = Form(...),
    url: str = Form(...),
    target_price: str = Form(...),
    selector: str = Form(""),
):
    name, url, selector = name.strip()[:200], url.strip(), selector.strip()
    target = clean_price_string(target_price)

    if not name:
        return redirect("Informe um nome para o produto.", "err")
    if target is None or target <= 0:
        return redirect("Preço alvo inválido.", "err")
    if not is_safe_url(url):
        return redirect("URL inválida ou não permitida.", "err")
    if selector and not is_valid_selector(selector):
        return redirect("Seletor CSS inválido.", "err")

    with SessionLocal() as session:
        product = Product(name=name, url=url, target_price=target, price_selector=selector or None)
        session.add(product)
        session.commit()
        product_id = product.id

    background.add_task(check_product_by_id, product_id)
    return redirect(f"'{name}' adicionado. A primeira verificação está em andamento.")


@router.post("/products/{product_id}/toggle", dependencies=post_guard)
def toggle_product(product_id: int):
    with SessionLocal() as session:
        product = session.get(Product, product_id)
        if product is None:
            return redirect("Produto não encontrado.", "err")
        product.active = not product.active
        session.commit()
    return redirect()


@router.post("/products/{product_id}/target", dependencies=post_guard)
def update_target(product_id: int, target_price: str = Form(...)):
    target = clean_price_string(target_price)
    if target is None or target <= 0:
        return redirect("Preço alvo inválido.", "err")

    with SessionLocal() as session:
        product = session.get(Product, product_id)
        if product is None:
            return redirect("Produto não encontrado.", "err")
        product.target_price = target
        session.commit()
    return redirect("Preço alvo atualizado.")


@router.post("/products/{product_id}/delete", dependencies=post_guard)
def delete_product(product_id: int):
    with SessionLocal() as session:
        product = session.get(Product, product_id)
        if product is None:
            return redirect("Produto não encontrado.", "err")
        session.delete(product)
        session.commit()
    return redirect("Produto removido.")


@router.post("/search", response_class=HTMLResponse, dependencies=post_guard)
def search(request: Request, q: str = Form(...), ai_filter: bool = Form(False)):
    q = q.strip()
    if not q:
        return redirect("Digite o que deseja buscar.", "err")

    try:
        offers = search_offers(q)
    except SearchError as exc:
        return redirect(str(exc), "err")

    note = ""
    if ai_filter and ai_enabled() and offers:
        kept = filter_matching_offers(q, [offer.title for offer in offers])
        if kept is None:
            note = "Não foi possível filtrar com IA; mostrando todas as ofertas."
        else:
            offers = [offers[i] for i in kept]

    prices = [offer.price for offer in offers]
    return templates.TemplateResponse(
        request,
        "search.html",
        {
            "q": q,
            "offers": offers,
            "note": note,
            "suggested": _suggested_target(min(prices)) if prices else "",
        },
    )


@router.post("/search/add", dependencies=post_guard)
def add_offers(
    background: BackgroundTasks,
    target_price: str = Form(...),
    sel: list[int] = Form(default=[]),
    titles: list[str] = Form(default=[]),
    links: list[str] = Form(default=[]),
    stores: list[str] = Form(default=[]),
):
    target = clean_price_string(target_price)
    if target is None or target <= 0:
        return redirect("Preço alvo inválido.", "err")
    if not sel:
        return redirect("Selecione ao menos uma oferta.", "err")
    if not (len(titles) == len(links) == len(stores)):
        return redirect("Dados da busca inconsistentes; tente novamente.", "err")

    added, skipped, new_ids = 0, 0, []
    with SessionLocal() as session:
        existing = {url for (url,) in session.query(Product.url)}
        for index in sorted(set(sel))[:MAX_OFFERS_PER_REQUEST]:
            if not 0 <= index < len(links):
                continue
            link = links[index].strip()
            if link in existing or not is_safe_url(link):
                skipped += 1
                continue

            product = Product(
                name=f"{titles[index].strip()} ({stores[index].strip()})"[:200],
                url=link,
                target_price=target,
            )
            session.add(product)
            session.flush()
            new_ids.append(product.id)
            existing.add(link)
            added += 1
        session.commit()

    for product_id in new_ids:
        background.add_task(check_product_by_id, product_id)

    message = f"{added} oferta(s) adicionada(s)."
    if skipped:
        message += f" {skipped} ignorada(s) (já monitoradas ou URL inválida)."
    return redirect(message, "ok" if added else "err")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    if not ADMIN_PASSWORD:
        logger.warning("ADMIN_PASSWORD não definido: a interface web está desativada (o monitor segue rodando).")
    scheduler = build_scheduler()
    scheduler.start()
    try:
        yield
    finally:
        scheduler.shutdown(wait=False)


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.include_router(router)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "same-origin"
    return response


@app.get("/health", response_class=PlainTextResponse)
def health() -> str:
    return "ok"
