from datetime import datetime, timezone

from sqlalchemy import Boolean, Column, DateTime, Float, Integer, String, create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

from app.config import DATABASE_URL

Base = declarative_base()


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


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

    def __repr__(self) -> str:
        return f"<Product id={self.id} name={self.name!r} target={self.target_price}>"


def _normalized_database_url() -> str:
    # Railway/Heroku expõem "postgres://", que o SQLAlchemy 1.4+ não aceita.
    if DATABASE_URL.startswith("postgres://"):
        return DATABASE_URL.replace("postgres://", "postgresql://", 1)
    return DATABASE_URL


engine = create_engine(_normalized_database_url(), pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def init_db() -> None:
    Base.metadata.create_all(engine)
