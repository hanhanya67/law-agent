# -*- coding: utf-8 -*-
"""把 data/raw 下的消费/食安两部法全文，写进 `data/domain_statutes.jsonl`。

为什么需要这一步、而不是让 LawIndex 直接读 txt：
  接入内核语料是一个 JSONL，字段（law_short / article_no / chapter_path …）
  是 LawIndex 与引用图共同依赖的契约。txt 只是抓取的中间产物，方便人核对
  原文；索引侧只认 JSONL。

这一步做三件事：
1. 重新抓一次（`fetch_consumer_food.py` 的解析函数），拿到**章名**——
   txt 里只存了「第N条 + 正文」，章名丢了，而章名进 `_doc_text` 后能让
   「第四章 食品生产经营」这类结构性问法命中。
2. 清掉旧的消费/食安**要点摘录**行（id 与新全文行完全相同，不删会撞 id）。
3. 道交法那 4 条**摘录原样保留**——它们本来就是《道路交通安全法》的
   代表性条款，没有替代它的全文。

先备份再写。写完打印条数与分法统计，方便和 `--check` 的结果对照。
"""
from __future__ import annotations

import json
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import fetch_consumer_food as F  # noqa: E402

DOMAIN = ROOT / "data" / "domain_statutes.jsonl"

_DIGITS = "〇一二三四五六七八九"


def int2cn(n: int) -> str:
    """123 -> 一百二十三。条号只到四位数，不需要通用方案。"""
    if n < 10:
        return _DIGITS[n]
    if n < 20:
        return "十" + (_DIGITS[n % 10] if n % 10 else "")
    if n < 100:
        return _DIGITS[n // 10] + "十" + (_DIGITS[n % 10] if n % 10 else "")
    if n < 1000:
        rest = n % 100
        head = _DIGITS[n // 100] + "百"
        if rest == 0:
            return head
        if rest < 10:
            return head + "零" + _DIGITS[rest]
        return head + int2cn(rest)
    rest = n % 1000
    head = _DIGITS[n // 1000] + "千"
    if rest == 0:
        return head
    if rest < 100:
        return head + "零" + int2cn(rest)
    return head + int2cn(rest)


def build_rows() -> list[dict]:
    rows: list[dict] = []
    for t in F.TARGETS:
        text = F.fetch(t["url"])
        arts, problems = F.split_articles(text)
        if problems or len(arts) != t["expect"]:
            raise SystemExit(f"{t['short']} 校验失败：{problems} / {len(arts)} 条")
        for a in arts:
            no = int(a["no"])
            rows.append({
                "id": f"{t['short']}_{no}",
                "law_name": t["name"],
                "law_short": t["short"],
                "article_no": str(no),
                "article_no_cn": f"第{int2cn(no)}条",
                "chapter_path": a["chapter"],
                "content": a["content"],
                "status": "现行有效",
                "content_type": "全文",
                "note": t["note"],
            })
    return rows


def main() -> int:
    new_rows = build_rows()
    new_ids = {r["id"] for r in new_rows}
    laws = {r["law_short"] for r in new_rows}

    old: list[dict] = []
    if DOMAIN.is_file():
        for line in DOMAIN.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                old.append(json.loads(line))

    # 只替换这两部法；道交法摘录与其它内核的语料一律不动。
    kept = [r for r in old if r.get("law_short") not in laws]
    dropped = len(old) - len(kept)
    merged = kept + new_rows

    bak = DOMAIN.with_suffix(".jsonl.bak_" + time.strftime("%Y%m%d_%H%M%S"))
    shutil.copy2(DOMAIN, bak)

    seen: dict[str, int] = {}
    for r in merged:
        seen[r["law_short"]] = seen.get(r["law_short"], 0) + 1
    if len({r["id"] for r in merged}) != len(merged):
        raise SystemExit("合并后出现重复 id，拒绝写入")

    with DOMAIN.open("w", encoding="utf-8") as f:
        for r in merged:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    print(f"备份 {bak.name}")
    print(f"替换：删掉旧行 {dropped} 条，写入全文 {len(new_rows)} 条")
    print("分法统计：" + "，".join(f"{k} {v} 条" for k, v in sorted(seen.items())))
    print(f"总行数 {len(merged)}，文件 {DOMAIN.stat().st_size} 字节")
    return 0


if __name__ == "__main__":
    sys.exit(main())
