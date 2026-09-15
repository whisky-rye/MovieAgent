"""为历史会话里缺外部 id 的推荐卡片补 douban_id / imdb_id（用于拼详情页跳转链接）。

背景：外链功能上线前的旧消息落库时 items 里没有外部 id 字段，但 item["id"]
保留了统一标识（imdb:{imdb_id} / douban:{内部主键}）。因此：
- IMDb：id 即 imdb:{imdb_id}，直接拆出，零查询；
- 豆瓣：id 是内部主键，且主键 ≠ douban_id（真实 subject id），需按主键批量回查
  douban_main（一次 IN 查询，与 content_recall 同范式）。

查询失败（如 DB 不可用）静默降级为不补——外链按钮不渲染，不影响会话加载。
"""
from __future__ import annotations

import logging

from db.session import fetch_all

logger = logging.getLogger("api.backfill")


def backfill_item_ids(items: list) -> list:
    """就地补全 items 中缺失的 douban_id / imdb_id，返回原列表。"""
    if not items:
        return items

    douban_pks: list[int] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        iid = str(it.get("id") or "")
        if iid.startswith("imdb:"):
            if not it.get("imdb_id"):
                it["imdb_id"] = iid.split(":", 1)[1]
        elif iid.startswith("douban:"):
            if not it.get("douban_id"):
                try:
                    douban_pks.append(int(iid.split(":", 1)[1]))
                except ValueError:
                    continue

    if not douban_pks:
        return items

    try:
        rows = fetch_all(
            "SELECT id, douban_id, imdb_id FROM douban_main WHERE id IN :pks",
            {"pks": douban_pks},
        )
    except Exception as exc:  # DB 不可用时降级：只影响外链，不影响会话
        logger.warning("backfill douban_id failed (degrade without links): %s", exc)
        return items

    dm = {r["id"]: r for r in rows}
    for it in items:
        if not isinstance(it, dict):
            continue
        iid = str(it.get("id") or "")
        if iid.startswith("douban:") and (not it.get("douban_id") or not it.get("imdb_id")):
            try:
                r = dm.get(int(iid.split(":", 1)[1]))
            except ValueError:
                continue
            if r:
                if not it.get("douban_id"):
                    it["douban_id"] = str(r["douban_id"] or "")
                if not it.get("imdb_id"):
                    it["imdb_id"] = str(r["imdb_id"] or "")
    return items
