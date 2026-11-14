# -*- coding: utf-8 -*-
"""抓取《消费者权益保护法》《食品安全法》全文，写成 data/raw/ 下的纯文本。

为什么单独一个脚本、而不是塞进 import_law_text.py：
  这两部法进的是 `data/domain_statutes.jsonl`（接入内核语料），不是主索引
  `data/statutes.jsonl`。主索引的 2066 条与 BM25 分数是被 31 个老套件逐位
  钉死的，任何一次「顺手多导点语料」都会让劳动法内核的检索排名漂移。

为什么要这两部法：
  消费·食安内核原来只有 8 条要点摘录，是五个内核里唯一「拿演示语料当语料」
  的一个。问「预付卡跑路能不能退」「标签瑕疵算不算十倍赔偿」时，摘录里
  根本没有对应条文，只能拒答——而拒答的理由是「本库未收录」，不是
  「法条不支持」，对用户是误导。

来源与版本（2026-11-06 实测可直连）：
  消费者权益保护法  gov.cn 主席令第七号（2013-10-25 第二次修正）   63 条
  食品安全法        gov.cn 主席令第二十一号（2015-04-24 修订）     154 条

  食安法在 2018、2021 年另有修正（涉及个别条款，多为机构名称与处罚
  衔接）。本脚本抓到的 2015 修订版在**条文主体**上与现行文本一致，
  但每条都标了修正提示，不冒充「最新全文」——这一点写进 JSONL 的 note。

校验（和 fetch_civil_code.py 同一条铁律）：
  条号必须从 1 连续到 N 且不重复。少一页、多一段引用、正文里出现
  「依照本法第 X 条」都会让序列断掉，对不上就不写文件——宁可没抓到，
  也不要抓一份缺条的语料去回答法律问题。

用法：
    python scripts/fetch_consumer_food.py            # 抓取并写 data/raw/*.txt
    python scripts/fetch_consumer_food.py --check    # 只校验，不写文件
"""
from __future__ import annotations

import argparse
import re
import ssl
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw"

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"}

CN = "〇零一二三四五六七八九十百千两"
_CN_DIGIT = {"〇": 0, "零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
             "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_CN_UNIT = {"十": 10, "百": 100, "千": 1000}


def cn2int(s: str) -> int:
    if s.isdigit():
        return int(s)
    total = section = num = 0
    for ch in s:
        if ch in _CN_DIGIT:
            num = _CN_DIGIT[ch]
        elif ch in _CN_UNIT:
            u = _CN_UNIT[ch]
            section += (num or 1) * u
            num = 0
    return total + section + num


def fetch(url: str) -> str:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    req = urllib.request.Request(url, headers=UA)
    raw = urllib.request.urlopen(req, context=ctx, timeout=30).read()
    html = raw.decode("utf-8", "replace")
    # 先掐掉脚本与样式，再退掉标签。政府网正文里的缩进是 &#160; 实体，
    # 不还原成空格的话，「第一条」不会落在行首，正则一条都匹配不到。
    html = re.sub(r"<script.*?</script>", "", html, flags=re.S)
    html = re.sub(r"<style.*?</style>", "", html, flags=re.S)
    text = re.sub(r"<[^>]+>", "\n", html)
    text = (text.replace("&#160;", " ").replace("&nbsp;", " ")
                .replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">"))
    lines = [ln.strip() for ln in text.split("\n")]
    return "\n".join(ln for ln in lines if ln)


_ART = re.compile(r"第([" + CN + r"]{1,6})条")
_CHAP = re.compile(r"第([" + CN + r"]{1,3})([章节])\s*([^\n]{0,40})")

# 页面尾部与条文本体无关的模板文字。末条后面一定跟着「相关链接 / 责任编辑」
# 这类东西——不切掉，`第六十三条` 的正文就会以「责任编辑： 曹乐溪」结尾，
# 而这段文字会被当成法条原文进索引，污染检索与引用。
_CHROME = ("相关链接", "责任编辑", "【E-mail推荐", "打印本页", "关闭窗口",
           "扫一扫", "分享到", "版权所有", "网站地图", "&#183;", "&#160;",
           "上一篇", "下一篇", "返回顶部")


def _sanitize(body: str) -> str:
    """去掉正文里的模板残留与没解干净的实体。"""
    cut = len(body)
    for marker in _CHROME:
        i = body.find(marker)
        if 0 <= i < cut:
            cut = i
    body = body[:cut]
    body = re.sub(r"&#\d+;|&[a-zA-Z]+;", "", body)
    body = re.sub(r"[ \u3000]{2,}", " ", body)
    return body.strip()


def split_articles(text: str) -> tuple[list[dict[str, str]], list[str]]:
    """按「第X条」切条，并校验条号连续。

    难点：条文正文里会出现「依照本法第五十五条」「前款第（一）项」这类引用，
    直接全文 findall 会多出一堆假条号。做法是——只接受「比上一条正好大 1」
    的标记，其余一律当作正文内容跳过。这样既能跳过引用，也能在真的缺条时
    立刻停下（下一段跳到的条号不会是 prev+1）。
    """
    marks: list[tuple[int, int, str]] = []   # (start, end, 中文条号)
    for m in _ART.finditer(text):
        marks.append((m.start(), m.end(), m.group(1)))

    chapters: list[tuple[int, str]] = []
    for m in _CHAP.finditer(text):
        label = f"第{m.group(1)}{m.group(2)} {m.group(3).strip()}".strip()
        chapters.append((m.start(), label))

    picked: list[tuple[int, int, int]] = []  # (num, start, end)
    expect = 1
    for start, end, cn in marks:
        n = cn2int(cn)
        if n == expect:
            picked.append((n, start, end))
            expect += 1

    problems: list[str] = []
    if not picked:
        return [], ["一条都没切出来：页面结构可能变了"]
    if picked[-1][0] != expect - 1:
        problems.append(f"条号不连续：最后一条是 {picked[-1][0]}")
    if len(picked) != expect - 1:
        problems.append(f"条数不符：切出 {len(picked)}，序号应到 {expect - 1}")

    out: list[dict[str, str]] = []
    for i, (num, start, end) in enumerate(picked):
        body_end = picked[i + 1][1] if i + 1 < len(picked) else len(text)
        body = text[end:body_end].strip()
        body = re.sub(r"\s*\n\s*", "", body)          # 段内换行合并
        body = _sanitize(body)
        chap = ""
        for pos, label in chapters:
            if pos < start:
                chap = label
            else:
                break
        out.append({"no": str(num), "content": body, "chapter": chap})
    return out, problems


TARGETS = (
    {
        "short": "消费者权益保护法",
        "name": "中华人民共和国消费者权益保护法",
        "expect": 63,
        "url": "https://www.gov.cn/ldhd/2013-10/25/content_2515324.htm",
        "note": "2013 年第二次修正版全文（现行有效）。",
    },
    {
        "short": "食品安全法",
        "name": "中华人民共和国食品安全法",
        "expect": 154,
        "url": "https://www.gov.cn/zhengce/2015-04/25/content_2853643.htm",
        "note": ("2015 年修订版全文。该法 2018、2021 年另有修正（涉及个别条款，"
                 "多为机构名称与处罚衔接）；引用具体条文时请核对官方最新文本。"),
    },
)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只校验，不写文件")
    args = ap.parse_args()

    failed = False
    for t in TARGETS:
        try:
            text = fetch(t["url"])
        except Exception as e:                    # noqa: BLE001
            print(f"[FAIL] {t['short']} 抓取失败：{type(e).__name__} {e}")
            failed = True
            continue
        arts, problems = split_articles(text)
        if problems:
            print(f"[FAIL] {t['short']}：" + "；".join(problems))
            failed = True
            continue
        if len(arts) != t["expect"]:
            print(f"[FAIL] {t['short']}：切出 {len(arts)} 条，应为 {t['expect']} 条")
            failed = True
            continue
        print(f"[PASS] {t['short']}：{len(arts)} 条，章 {len({a['chapter'] for a in arts})} 个")
        print(f"       第一条：{arts[0]['content'][:40]}")
        print(f"       末一条：{arts[-1]['content'][:40]}")
        if args.check:
            continue
        # 纯文本落盘，格式与 data/raw 下其它法条一致：一行一条，条号在前。
        lines = []
        for a in arts:
            lines.append(f"第{a['no']}条　{a['content']}")
        out = RAW / f"{t['short']}.txt"
        out.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"       已写出 {out.relative_to(ROOT)}（{out.stat().st_size} 字节）")

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
