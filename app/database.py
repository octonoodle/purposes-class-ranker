from collections.abc import Generator
import os

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, declarative_base, sessionmaker

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+psycopg://classranker:classranker@/classranker?host=/tmp",
)

# PostgreSQL provides row-level locking and concurrent transaction handling.
engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
Base = declarative_base()


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def wait_for_db_ready() -> None:
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))
