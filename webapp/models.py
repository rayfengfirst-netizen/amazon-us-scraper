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
    asin: str = Field(index=True, max_length=16)
    original_input: str = Field(max_length=2048)
    status: str = Field(default="pending", max_length=32)  # pending, running, success, failed
    result_json: Optional[str] = Field(default=None)
    error_message: Optional[str] = Field(default=None, max_length=4096)
    collect_via: Optional[str] = Field(default=None, max_length=16)  # api | cache
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)
