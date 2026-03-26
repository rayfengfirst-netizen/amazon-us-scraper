from __future__ import annotations

from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import inspect, text
from sqlmodel import SQLModel, create_engine, Session

# 项目根目录下的 data/
PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")
DATA_DIR = PROJECT_ROOT / "data"
_DB_PATH = DATA_DIR / "app.db"

engine = create_engine(
    f"sqlite:///{_DB_PATH}",
    connect_args={"check_same_thread": False},
)


def _migrate_sqlite() -> None:
    """为已有 SQLite 库补充新增列（create_all 不会改旧表结构）。"""
    if engine.dialect.name != "sqlite":
        return
    insp = inspect(engine)
    if insp.has_table("target"):
        cols = {c["name"] for c in insp.get_columns("target")}
        if "collect_via" not in cols:
            with engine.connect() as conn:
                conn.execute(text("ALTER TABLE target ADD COLUMN collect_via VARCHAR(16)"))
                conn.commit()
    if insp.has_table("shopify_shop"):
        scols = {c["name"] for c in insp.get_columns("shopify_shop")}
        with engine.connect() as conn:
            if "oauth_client_id" not in scols:
                conn.execute(text("ALTER TABLE shopify_shop ADD COLUMN oauth_client_id VARCHAR(128)"))
            if "oauth_client_secret" not in scols:
                conn.execute(text("ALTER TABLE shopify_shop ADD COLUMN oauth_client_secret VARCHAR(256)"))
            conn.commit()


def init_db() -> None:
    from webapp.models import AsinSnapshot, ShopifyPublishLog, ShopifyShop, Target  # noqa: F401

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SQLModel.metadata.create_all(engine)
    _migrate_sqlite()


def get_session() -> Session:
    return Session(engine)
