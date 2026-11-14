# -*- coding: utf-8 -*-
"""抓取《中华人民共和国民法典》全文（1260 条），写成 data/raw/民法典.txt。

为什么自己抓：
  flk.npc.gov.cn 已改成前端单页应用，旧 JSON 接口返回首页 HTML；
  中国人大网的老文章 URL 全部 302 到首页；gov.cn 2020 年那批链接也 404 了。
  央广网这篇受权发布稿是少数还能直接拿到纯文本全文的源，且按编分了 7 页。

分页规律（实测，与编章结构一一对应）：
  主页面   第一条   — 第二百零四条    总则      204 条
  _1      第二百零五条 — 第四百六十二条  物权      258 条
  _2      第四百六十三条 — 第九百八十八条  合同      526 条
  _3      第九百八十九条 — 第一千零三十九条  人格权     51 条
  _4      第一千零四十条 — 第一千一百一十八条 婚姻家庭    79 条
  _5      第一千一百一十九条 — 第一千一百六十三条 继承      45 条
  _6      第一千一百六十四条 — 第一千二百六十条 侵权+附则   97 条
  合计 1260 条

注意页码顺序不直观：物权的后缀是 `_1`，合同是 `_2`。少抓 `_1` 会安静地丢掉
258 条——本项目正是靠「条号必须连续递增」发现这个坑的（切分器要求第 N 条
之后必须紧跟第 N+1 条，缺一段就切不下去，条数立刻偏少）。

用法：
  python scripts/fetch_civil_code.py              # 抓取并写 data/raw/民法典.txt
  python scripts/fetch_civil_code.py --check      # 只校验，不写文件
"""
from __future__ import annotations

import argparse
import re
import ssl
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "raw" / "民法典.txt"

BASE = "https://news.cnr.cn/native/gd/20200602/t20200602_525113208{suf}.shtml"
SUFFIXES = ["", "_1", "_2", "_3", "_4", "_5", "_6"]
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"}

CN = "零一二三四五六七八九十百千两"
_CN_DIGIT = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
             "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_CN_UNIT = {"十": 10, "百": 100, "千": 1000}


def cn2int(s: str) -> int:
    s = s.strip()
    if s.isdigit():
        return int(s)
    total, num = 0, 0
    for ch in s:
        if ch in _CN_DIGIT:
            num = _CN_DIGIT[ch]
        elif ch in _CN_UNIT:
            unit = _CN_UNIT[ch]
            if unit == 10 and num == 0:
                num = 1
            total += num * unit
            num = 0
        else:
            raise ValueError(f"非法中文数字：{s}")
    return total + num


ART_RE = re.compile(rf"^第([{CN}]+)条[\s\u3000]*(.*)$", re.S)
# 分编要排在「编」前面判断，否则「第一分编　通则」永远匹配不上
HEAD_RE = re.compile(rf"^第[{CN}]+(?:分编|编|章|节)[\s\u3000]|^附\s*则\s*$")
# 页面尾巴上的噪音，别混进语料
NOISE_RE = re.compile(r"(编辑[：:]|责任编辑|免责声明|扫一扫|版权声明|返回顶部|"
                      r"两会受权发布|来源[：:]|原标题)")


def fetch(url: str) -> str:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=30, context=ctx) as r:
        raw = r.read()
    for enc in ("gb18030", "utf-8"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "ignore")


def page_lines(text: str, first: bool = False) -> list[str]:
    """把一页 HTML 变成「条文行 + 章节标题行」的有序列表。

    只收两类行：
      · 以「第X条」开头的条文行（含同段内的各款）
      · 以「第X编 / 第X分编 / 第X章 / 第X节 / 附则」开头的标题行

    第一页开头有一份完整的目录（编章节全列一遍），如果直接保留，每个条文的
    章节路径都会指到目录上——《劳动合同法》导入时踩过这个坑。这里不能按
    「标题文字去重」处理，因为「第一分编　通则」在物权编和合同编各出现一次，
    去重会误删物权编那一份。改成定位正文起点：第一条之前最后一个「第X编」
    就是正文的编头，它前面的全是目录，整段丢掉。
    """
    body = re.sub(r"<script.*?</script>", " ", text, flags=re.S)
    body = re.sub(r"<style.*?</style>", " ", body, flags=re.S)
    body = re.sub(r"<[^>]+>", "\n", body)
    out: list[str] = []
    started = False
    for ln in body.split("\n"):
        ln = ln.replace("\u00a0", " ").strip()
        if not ln:
            continue
        if ART_RE.match(ln):
            started = True
            out.append(ln)
        elif HEAD_RE.match(ln):
            if started:              # 非首页：标题行直接保留
                out.append(ln)
            elif not first:
                out.append(ln)
        elif started and len(ln) >= 12 and not NOISE_RE.search(ln):
            # 兜底：条文被拆成多个 <p> 时，续段接在上一条后面
            if out and ART_RE.match(out[-1]):
                out[-1] = out[-1] + "\n" + ln

    if first:
        ia = next((i for i, l in enumerate(out) if ART_RE.match(l)), 0)
        start = 0
        for i in range(ia - 1, -1, -1):
            if re.match(rf"^第[{CN}]+编[\s\u3000]", out[i]):
                start = i          # 正文的「第一编　总则」，目录里的同类行更靠前
                break
        out = out[start:]
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只校验，不写文件")
    args = ap.parse_args()

    all_lines: list[str] = []
    for suf in SUFFIXES:
        url = BASE.format(suf=suf)
        try:
            lines = page_lines(fetch(url), first=(suf == ""))
        except Exception as e:                      # noqa: BLE001
            print(f"[FAIL] {url} -> {type(e).__name__}: {e}")
            return 1
        arts = [l for l in lines if ART_RE.match(l)]
        span = ""
        if arts:
            first = cn2int(ART_RE.match(arts[0]).group(1))
            last = cn2int(ART_RE.match(arts[-1]).group(1))
            span = f"{first}–{last}（{last - first + 1} 条）"
        print(f"  页 {suf or '(主)':<4} 抽到 {len(arts):>3} 条  实际条号 {span}")
        all_lines.extend(lines)

    # ---- 校验：条号必须从 1 连续递增到 1260 ----
    seen: dict[int, str] = {}
    order: list[str] = []
    for ln in all_lines:
        m = ART_RE.match(ln)
        if not m:
            order.append(ln)
            continue
        n = cn2int(m.group(1))
        if n in seen:
            print(f"[warn] 第 {n} 条重复出现，已保留先出现的一条")
            continue
        seen[n] = ln
        order.append(ln)

    nums = sorted(seen)
    missing = [i for i in range(1, 1261) if i not in seen]
    print(f"\n共抽到 {len(seen)} 条，缺 {len(missing)} 条")
    if missing:
        print(f"  缺失条号：{missing[:30]}{' ...' if len(missing) > 30 else ''}")
    if nums:
        print(f"  条号范围：{nums[0]} – {nums[-1]}")

    # 抽查几条已知内容
    probes = {
        1: "根据宪法，制定本法",
        585: "违约金",
        1079: "离婚",
        1254: "抛掷物品",
        1260: "同时废止",
    }
    print("\n抽查：")
    for n, kw in probes.items():
        t = seen.get(n, "")
        ok = kw in t
        print(f"  {'✔' if ok else '✘'} 第{n}条  含「{kw}」  {len(t)} 字  {t[:40]}")

    if missing:
        print("\n[abort] 有缺失，不写文件")
        return 1
    if args.check:
        print("\n[check] 校验通过（未写文件）")
        return 0

    header = (
        "中华人民共和国民法典\n\n"
        "（2020年5月28日第十三届全国人民代表大会第三次会议通过）\n\n"
    )
    OUT.write_text(header + "\n".join(order) + "\n", encoding="utf-8")
    print(f"\n已写入 {OUT}（{OUT.stat().st_size / 1024:.0f} KB）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
