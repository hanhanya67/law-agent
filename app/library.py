# -*- coding: utf-8 -*-
"""法典图书馆 —— 按内核分库读原文，带目录树、条号直达与快速查找（v139）。

这一层刻意**不碰主索引**：`get_profile_index()` 已经把「一个内核 = 一份
语料集合」做完了，`LawIndex.articles / by_id / by_law / search` 全是现成的。
图书馆只是把它**按书目结构重新组织**一遍，不重建 BM25、不改 IDF、不写库。
所以劳动法那 2066 条的分数仍逐位不变，`test_profiles.py` 的断言不受影响。

三条边界：
  1. **按 pid 隔离**：每本书、每条原文都从该内核的索引里取，`article()`
     用 `by_id` 校验归属，跨内核传 id 一律取不到，不会串库。
  2. **全文 / 摘录分开说**：道交法 4 条是「要点摘录」，语料里带
     `content_type` / `note`，原样回给前端标注，不冒充全文。
  3. **只读**：不写任何表，没有归属之争；接口仍需登录（法条原文是交付内容，
     不放到公开接口上）。

目录树为什么按 `chapter_path` 分组：语料里这个字段本来就有，而且民法典、
消保法都是按章写入的。少部分司法解释没有分章（空串），归到「（未分章）」，
不假装它有一章。顺序按「该章第一条在原语料里的位置」排，法条天然是按
条号顺序存的，不用自己再猜章序。
"""
from __future__ import annotations

import re
from typing import Any

from . import profiles as _profiles
from .retrieval import LawIndex, cn2int, get_profile_index

_NO_CHAPTER = "（未分章）"
# 「第585条」「第五百八十五条」「585」都要能直达。中文数字交给 cn2int。
_RE_ART = re.compile(r"^\s*(?:第)?\s*([0-9零一二三四五六七八九十百千]+)\s*条?\s*$")
_RE_CN_ONLY = re.compile(r"^[零一二三四五六七八九十百千]+$")


def profile_for(pid: Any) -> tuple[Any, LawIndex]:
    """pid → (内核配置, 该内核的索引)。非法 pid 回落劳动法，和全局同一口径。"""
    p = _profiles.resolve(pid)
    return p, get_profile_index(p)


def _article_key(a: dict) -> tuple[int, str]:
    """条号排序：能转数字就按数字，转不了（如「附则」「之一」）排后面。"""
    raw = str(a.get("article_no") or "").strip()
    try:
        return (0, f"{int(raw):08d}")
    except ValueError:
        return (1, raw)


def _article_no(a: dict) -> int | None:
    try:
        return int(str(a.get("article_no") or "").strip())
    except ValueError:
        return None


def books(pid: Any) -> dict[str, Any]:
    """书目 + 目录树。**不含正文**，正文走 /api/library/law 按需拉。

    民法典 1260 条，一次把全部原文塞进目录接口的返回体没有意义；目录只要
    「哪部法、几章、每章哪些条」，前端默认展开第一部法再拉全文。
    """
    p, idx = profile_for(pid)
    laws: list[dict[str, Any]] = []
    for law_short, arts in idx.by_law.items():
        # 按 law_short 分组后保留语料顺序；章序按该章第一次出现的位置。
        chapters: dict[str, list[dict]] = {}
        order: list[str] = []
        for a in arts:
            ch = str(a.get("chapter_path") or "").strip() or _NO_CHAPTER
            if ch not in chapters:
                chapters[ch] = []
                order.append(ch)
            chapters[ch].append(a)
        ch_out = []
        for ch in order:
            items = sorted(chapters[ch], key=_article_key)
            ch_out.append({
                "path": ch,
                "count": len(items),
                "articles": [{
                    "id": a["id"],
                    "no": a["article_no"],
                    "no_cn": a.get("article_no_cn", f"第{a['article_no']}条"),
                } for a in items],
            })
        first = arts[0]
        laws.append({
            "law_short": law_short,
            "law_name": first.get("law_name") or law_short,
            "count": len(arts),
            # 摘录与全文不能混着说：整部法只要有一条标了摘录，书目就标注。
            "content_type": first.get("content_type") or "全文",
            "note": first.get("note") or "",
            "chapters": ch_out,
        })
    laws.sort(key=lambda x: (-x["count"], x["law_short"]))
    return {
        "pid": p.id,
        "name": p.name,
        "law_count": len(laws),
        "article_count": len(idx.articles),
        "laws": laws,
    }


def law_text(pid: Any, law_short: str) -> dict[str, Any]:
    """某部法的全部原文。目录点击跳转前先把这一部渲染出来。"""
    p, idx = profile_for(pid)
    arts = idx.by_law.get(str(law_short or "").strip())
    if not arts:
        return {"pid": p.id, "law_short": law_short, "articles": [], "count": 0}
    items = sorted(arts, key=_article_key)
    return {
        "pid": p.id,
        "law_short": law_short,
        "law_name": items[0].get("law_name") or law_short,
        "count": len(items),
        "articles": [_public(a) for a in items],
    }


def article(pid: Any, article_id: str) -> dict[str, Any] | None:
    """单条原文。只认该内核索引里的 id，跨内核一律 None。"""
    _, idx = profile_for(pid)
    a = idx.by_id.get(str(article_id or "").strip())
    return _public(a) if a else None


def _public(a: dict) -> dict[str, Any]:
    """回给前端的字段白名单。语料里还有 hierarchy_level / source_url 等，
    图书馆只展示跟「读条文」有关的，别把内部字段一起漏出去。"""
    return {
        "id": a.get("id"),
        "law_name": a.get("law_name"),
        "law_short": a.get("law_short"),
        "article_no": a.get("article_no"),
        "article_no_cn": a.get("article_no_cn"),
        "chapter_path": a.get("chapter_path") or "",
        "content": a.get("content") or "",
        "status": a.get("status") or "现行有效",
        "effective_date": a.get("effective_date") or "",
        "content_type": a.get("content_type") or "全文",
        "note": a.get("note") or "",
        "source_url": a.get("source_url") or "",
    }


def parse_article_no(q: str) -> int | None:
    """把用户输入的条号写法归一成数字。认不出来返回 None（走关键词检索）。"""
    m = _RE_ART.match(str(q or "").strip())
    if not m:
        return None
    raw = m.group(1)
    if raw.isdigit():
        return int(raw)
    if not _RE_CN_ONLY.match(raw):
        return None
    n = cn2int(raw)
    return n or None


def search(pid: Any, q: str, limit: int = 8) -> dict[str, Any]:
    """快速查找：条号直达优先，否则用该内核的 BM25 检索。

    为什么条号不走 BM25：「第 585 条」这种短串在 BM25 里几乎必然被一堆
    含「第五百八十五条」的交叉引用条文淹没。用户点名条号时要的是精确命中。
    跨内核分数不可比这件事在这里无所谓——只在**同一内核内部**排序。
    """
    p, idx = profile_for(pid)
    q = str(q or "").strip()[:80]
    if not q:
        return {"pid": p.id, "mode": "empty", "items": [], "note": "请输入条号或关键词"}
    n = parse_article_no(q)
    hits: list[dict] = []
    mode = "keyword"
    if n is not None:
        mode = "article_no"
        # 同一条号在不同法里都有；按书目顺序给，用户自己看是哪部法。
        for a in idx.articles:
            if _article_no(a) == n:
                hits.append(_public(a))
        hits = hits[: max(1, min(int(limit), 30))]
        if hits:
            return {"pid": p.id, "mode": mode, "items": hits,
                    "note": f"条号直达：命中 {len(hits)} 条"}
        # 点名的条号在本内核没有 → 明说，不悄悄降级成关键词凑结果。
        return {"pid": p.id, "mode": mode, "items": [],
                "note": f"本内核未收录第 {n} 条；可换个关键词检索正文"}
    for _score, a in idx.search(q, topk=max(1, min(int(limit), 20)),
                                only_valid=False):
        hits.append(_public(a))
    return {"pid": p.id, "mode": mode, "items": hits,
            "note": f"按正文检索到 {len(hits)} 条（仅本内核语料）" if hits
                    else "本内核语料里没有匹配条文"}
