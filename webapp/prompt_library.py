from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
LIB_FILE = ROOT / "prompts" / "shopify_openai" / "libraries.json"


def _load_raw() -> list[dict[str, Any]]:
    if not LIB_FILE.exists():
        return []
    data = json.loads(LIB_FILE.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        return []
    out: list[dict[str, Any]] = []
    for row in data:
        if isinstance(row, dict) and row.get("id") and row.get("prompts"):
            out.append(row)
    return out


def list_prompt_libraries() -> list[dict[str, Any]]:
    rows = _load_raw()
    return [
        {
            "id": str(r.get("id")),
            "name": str(r.get("name") or r.get("id")),
            "zh_comment": str(r.get("zh_comment") or ""),
            "prompts": r.get("prompts") or {},
        }
        for r in rows
    ]


def get_prompt_library(library_id: str) -> dict[str, Any] | None:
    lid = (library_id or "").strip()
    if not lid:
        return None
    for row in list_prompt_libraries():
        if row["id"] == lid:
            return row
    return None

