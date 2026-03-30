from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlmodel import Field, SQLModel


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class AsinSnapshot(SQLModel, table=True):
    """按 ASIN 缓存一份结构化 JSON，避免重复调用 ScraperAPI。"""

    asin: str = Field(primary_key=True, max_length=16)
    result_json: str
    updated_at: datetime = Field(default_factory=_utcnow)
    images_synced_at: Optional[datetime] = Field(default=None)


class Target(SQLModel, table=True):
    """一条「目标」记录：先入库，再可单独触发采集。"""

    id: Optional[int] = Field(default=None, primary_key=True)
    source: str = Field(default="amazon", index=True, max_length=16)  # amazon | ebay
    asin: str = Field(index=True, max_length=16)
    original_input: str = Field(max_length=2048)
    status: str = Field(default="pending", max_length=32)  # pending, running, success, failed
    result_json: Optional[str] = Field(default=None)
    error_message: Optional[str] = Field(default=None, max_length=4096)
    collect_via: Optional[str] = Field(default=None, max_length=16)  # api | cache
    shopify_editor_json: Optional[str] = Field(default=None)
    shopify_ai_rewritten_at: Optional[datetime] = Field(default=None)
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)


class ShopifyShop(SQLModel, table=True):
    """可发布到的 Shopify 店铺（凭据存库，勿提交到 Git）。"""

    __tablename__ = "shopify_shop"

    id: Optional[int] = Field(default=None, primary_key=True)
    label: str = Field(max_length=128)
    shop_domain: str = Field(index=True, max_length=128)
    # 店铺后台「开发应用」复制的静态 token；若填写下方 OAuth 凭据则可留空
    admin_token: str = Field(default="", max_length=512)
    # Dev Dashboard 应用：用 client_credentials 换取 access_token（见 Shopify 文档）
    oauth_client_id: Optional[str] = Field(default=None, max_length=128)
    oauth_client_secret: Optional[str] = Field(default=None, max_length=256)
    api_version: str = Field(default="2025-01", max_length=32)
    created_at: datetime = Field(default_factory=_utcnow)


class ShopifyPublishLog(SQLModel, table=True):
    """详情页发布到 Shopify 的历史记录。"""

    __tablename__ = "shopify_publish_log"

    id: Optional[int] = Field(default=None, primary_key=True)
    target_id: int = Field(foreign_key="target.id", index=True)
    shop_id: int = Field(foreign_key="shopify_shop.id", index=True)
    shopify_product_id: Optional[int] = None
    shopify_product_handle: Optional[str] = Field(default=None, max_length=256)
    product_status: str = Field(max_length=16)
    publish_scope: str = Field(max_length=32)
    error_message: Optional[str] = Field(default=None, max_length=4096)
    report_json: Optional[str] = Field(default=None)
    created_at: datetime = Field(default_factory=_utcnow)


class UpcCode(SQLModel, table=True):
    """UPC 码池：发布成功后即标记为已使用，不可复用。"""

    __tablename__ = "upc_code"

    id: Optional[int] = Field(default=None, primary_key=True)
    code: str = Field(index=True, max_length=32, unique=True)
    used: bool = Field(default=False, index=True)
    used_target_id: Optional[int] = Field(default=None, foreign_key="target.id")
    used_shopify_product_id: Optional[int] = Field(default=None)
    created_at: datetime = Field(default_factory=_utcnow)
    used_at: Optional[datetime] = Field(default=None)


class EbaySnapshot(SQLModel, table=True):
    """按 eBay item_id 缓存一份结构化 JSON，避免重复调用 ScraperAPI。"""

    item_id: str = Field(primary_key=True, max_length=32)
    result_json: str
    updated_at: datetime = Field(default_factory=_utcnow)
    images_synced_at: Optional[datetime] = Field(default=None)
