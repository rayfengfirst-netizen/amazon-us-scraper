from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
LIB_FILE = ROOT / "prompts" / "shopify_openai" / "libraries.json"
META_FILE = ROOT / "prompts" / "shopify_openai" / "meta.json"
PROMPT_KEYS = ("title", "description", "seo_title", "seo_description")


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


def _save_raw(rows: list[dict[str, Any]]) -> None:
    LIB_FILE.parent.mkdir(parents=True, exist_ok=True)
    LIB_FILE.write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _load_meta() -> dict[str, Any]:
    if not META_FILE.exists():
        return {}
    try:
        data = json.loads(META_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_meta(meta: dict[str, Any]) -> None:
    META_FILE.parent.mkdir(parents=True, exist_ok=True)
    META_FILE.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _clear_stored_default_if_matches(library_id: str) -> None:
    meta = _load_meta()
    if str(meta.get("default_library_id") or "") != library_id:
        return
    meta.pop("default_library_id", None)
    _save_meta(meta)


def get_default_prompt_library_id() -> str:
    """
    全站默认提示词模板 ID：优先 meta.json；否则 default_v1；再否则列表第一项。
    """
    libs = list_prompt_libraries()
    ids = [str(x["id"]) for x in libs]
    if not ids:
        return "default_v1"
    meta = _load_meta()
    preferred = str(meta.get("default_library_id") or "").strip()
    if preferred in ids:
        return preferred
    if "default_v1" in ids:
        return "default_v1"
    return ids[0]


def set_default_prompt_library_id(library_id: str) -> None:
    lid = _norm_text(library_id)
    if not get_prompt_library(lid):
        raise ValueError("模板不存在")
    meta = _load_meta()
    meta["default_library_id"] = lid
    _save_meta(meta)


def _norm_text(v: Any) -> str:
    return str(v or "").strip()


def _slugify(text: str) -> str:
    s = _norm_text(text).lower()
    s = re.sub(r"[^a-z0-9]+", "_", s)
    s = s.strip("_")
    return s or "prompt_template"


def _next_library_id(rows: list[dict[str, Any]], name: str) -> str:
    base = _slugify(name)
    exists = {str(x.get("id") or "") for x in rows}
    if base not in exists:
        return base
    idx = 2
    while f"{base}_{idx}" in exists:
        idx += 1
    return f"{base}_{idx}"


def _normalize_library_payload(payload: dict[str, Any], *, fixed_id: str = "") -> dict[str, Any]:
    lib_id = _norm_text(fixed_id or payload.get("id"))
    name = _norm_text(payload.get("name")) or lib_id or "Prompt Template"
    zh_comment = _norm_text(payload.get("zh_comment"))
    prompts_in = payload.get("prompts") if isinstance(payload.get("prompts"), dict) else {}
    prompts: dict[str, dict[str, str]] = {}
    for key in PROMPT_KEYS:
        p = prompts_in.get(key) if isinstance(prompts_in.get(key), dict) else {}
        prompts[key] = {
            "zh_comment": _norm_text(p.get("zh_comment")),
            "template": str(p.get("template") or ""),
        }
    return {
        "id": lib_id[:128],
        "name": name[:200],
        "zh_comment": zh_comment[:500],
        "prompts": prompts,
    }


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


def create_prompt_library(payload: dict[str, Any]) -> None:
    rows = _load_raw()
    name = _norm_text(payload.get("name")) or "Prompt Template"
    generated_id = _next_library_id(rows, name)
    item = _normalize_library_payload(payload, fixed_id=generated_id)
    rows.append(item)
    _save_raw(rows)


def update_prompt_library(library_id: str, payload: dict[str, Any]) -> None:
    lid = _norm_text(library_id)
    rows = _load_raw()
    idx = next((i for i, x in enumerate(rows) if str(x.get("id")) == lid), -1)
    if idx < 0:
        raise ValueError("模板不存在")
    item = _normalize_library_payload(payload, fixed_id=lid)
    rows[idx] = item
    _save_raw(rows)


def delete_prompt_library(library_id: str) -> None:
    lid = _norm_text(library_id)
    rows = _load_raw()
    kept = [x for x in rows if str(x.get("id")) != lid]
    if len(kept) == len(rows):
        raise ValueError("模板不存在")
    _save_raw(kept)
    _clear_stored_default_if_matches(lid)

