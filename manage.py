"""CLI para gerenciar os produtos monitorados.

Exemplos:
    python manage.py add --name "Geladeira X" --url "https://..." --target-price 3499 --selector ".price"
    python manage.py list
    python manage.py deactivate 1
"""
import argparse

from app.config import DATABASE_URL  # noqa: F401  (garante o load do .env)
from app.db import Product, SessionLocal, init_db
from app.scraper import format_price_brl


def cmd_add(args) -> None:
    with SessionLocal() as session:
        product = Product(
            name=args.name,
            url=args.url,
            price_selector=args.selector,
            target_price=args.target_price,
            render_js=args.render_js,
        )
        session.add(product)
        session.commit()
        print(f"Produto criado: id={product.id} nome={product.name!r}")


def cmd_list(args) -> None:
    with SessionLocal() as session:
        query = session.query(Product).order_by(Product.id)
        if args.active_only:
            query = query.filter(Product.active.is_(True))
        for product in query:
            last = format_price_brl(product.last_price) if product.last_price is not None else "-"
            status = "ativo" if product.active else "inativo"
            print(
                f"[{product.id}] {product.name} | alvo={format_price_brl(product.target_price)} "
                f"| último={last} | {status} | {product.url}"
            )


def _get_or_report(session, product_id: int):
    product = session.get(Product, product_id)
    if product is None:
        print(f"Produto {product_id} não encontrado.")
    return product


def cmd_set_active(args, active: bool) -> None:
    with SessionLocal() as session:
        product = _get_or_report(session, args.id)
        if product is None:
            return
        product.active = active
        session.commit()
        print(f"Produto {product.id} agora está {'ativo' if active else 'inativo'}.")


def cmd_remove(args) -> None:
    with SessionLocal() as session:
        product = _get_or_report(session, args.id)
        if product is None:
            return
        session.delete(product)
        session.commit()
        print(f"Produto {args.id} removido.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Gerencia os produtos monitorados.")
    sub = parser.add_subparsers(dest="command", required=True)

    add = sub.add_parser("add", help="Adiciona um produto.")
    add.add_argument("--name", required=True)
    add.add_argument("--url", required=True)
    add.add_argument("--target-price", type=float, required=True)
    add.add_argument("--selector", default=None, help="Seletor CSS do preço (opcional).")
    add.add_argument("--render-js", action="store_true", help="Renderiza a página com Playwright.")
    add.set_defaults(func=cmd_add)

    lst = sub.add_parser("list", help="Lista os produtos.")
    lst.add_argument("--active-only", action="store_true")
    lst.set_defaults(func=cmd_list)

    for name, active in (("activate", True), ("deactivate", False)):
        cmd = sub.add_parser(name, help=f"{name.capitalize()} um produto.")
        cmd.add_argument("id", type=int)
        cmd.set_defaults(func=lambda args, a=active: cmd_set_active(args, a))

    rm = sub.add_parser("remove", help="Remove um produto.")
    rm.add_argument("id", type=int)
    rm.set_defaults(func=cmd_remove)

    return parser


def main() -> None:
    args = build_parser().parse_args()
    init_db()
    args.func(args)


if __name__ == "__main__":
    main()
