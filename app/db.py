from datetime import datetime, timezone

from sqlalchemy import Boolean, Column, DateTime, Float, ForeignKey, Integer, String, create_engine, inspect, text
from sqlalchemy.orm import declarative_base, relationship, sessionmaker

from app.config import DATABASE_URL

Base = declarative_base()


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class ProductGroup(Base):
    """Um produto acompanhado em várias lojas: cada loja é um Product (link) do grupo."""

    __tablename__ = "product_groups"

    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    target_price = Column(Float, nullable=False)
    created_at = Column(DateTime, default=utcnow, nullable=False)

    products = relationship("Product", back_populates="group", cascade="all")

    def __repr__(self) -> str:
        return f"<ProductGroup id={self.id} name={self.name!r} target={self.target_price}>"


class Product(Base):
    __tablename__ = "products"

    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    url = Column(String, nullable=False)
    price_selector = Column(String, nullable=True)
    target_price = Column(Float, nullable=False)
    active = Column(Boolean, default=True, nullable=False)
    render_js = Column(Boolean, default=False, nullable=False)
    last_price = Column(Float, nullable=True)
    last_checked_at = Column(DateTime, nullable=True)
    last_alert_sent_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=utcnow, nullable=False)
    last_error = Column(String, nullable=True)
    group_id = Column(Integer, ForeignKey("product_groups.id", ondelete="SET NULL"), nullable=True)

    group = relationship("ProductGroup", back_populates="products")

    def __repr__(self) -> str:
        return f"<Product id={self.id} name={self.name!r} target={self.target_price}>"


def _normalized_database_url() -> str:
    # Railway/Heroku expõem "postgres://", que o SQLAlchemy 1.4+ não aceita.
    if DATABASE_URL.startswith("postgres://"):
        return DATABASE_URL.replace("postgres://", "postgresql://", 1)
    return DATABASE_URL


engine = create_engine(_normalized_database_url(), pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


# create_all não altera tabelas existentes: colunas novas entram por ALTER TABLE em bancos já criados.
NEW_PRODUCT_COLUMNS = {
    "group_id": "INTEGER REFERENCES product_groups (id) ON DELETE SET NULL",
    "last_error": "VARCHAR",
}


def _add_missing_columns() -> None:
    existing = {column["name"] for column in inspect(engine).get_columns("products")}
    with engine.begin() as connection:
        for name, ddl in NEW_PRODUCT_COLUMNS.items():
            if name not in existing:
                connection.execute(text(f"ALTER TABLE products ADD COLUMN {name} {ddl}"))


def init_db() -> None:
    Base.metadata.create_all(engine)
    _add_missing_columns()
