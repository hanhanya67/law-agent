# -*- coding: utf-8 -*-
"""抓取《中华人民共和国合同法》全文（428 条），写成 data/raw/合同法.txt。

为什么要收一部已废止的法：
  评测集里有 6 条版本题（v01–v06）问的是「合同法第114条还有效吗 / 现在还能不能引」。
  要答这类题，语料里必须同时存在「旧条文 + 它被谁替代」，缺一半就只能拒答。
  所以合同法按 `status=已废止` 入库，第 114 条另外在 manifest 里挂
  `repealed_note` 指向民法典第 585 条——整部废止 + 条级替代关系分开表达。

来源说明：
  gov.cn / npc.gov.cn 的老文章链接全部失效（302 到首页或 404），
  flk.npc.gov.cn 改成前端单页应用、旧 JSON 接口已不可用。
  这里用永济市人民政府门户网站收录的版本（.gov.cn 域名），
  条目结构与条数（428）与官方一致，切分器会逐条校验连续性。

页面结构（实测）：
  <div class="catalog_text">            目录，整块跳过
  <div class="f-article-title-large">   编 / 分则 / 附则
  <div class="f-article-title-small">   第X章
  <p class="f-article-title-tiny">      条文头（条号在 <span> 里）
  <p>（无 class）                       条文的第二款、第三款

用法：
  python scripts/fetch_contract_law.py
  python scripts/fetch_contract_law.py --check
"""
from __future__ import annotations

import argparse
import re
import ssl
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "raw" / "合同法.txt"
URL = "https://www.yongji.gov.cn/doc/2021/05/31/10322931.shtml"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"}

CN = "零一二三四五六七八九十百千两"
_CN_DIGIT = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
             "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_CN_UNIT = {"十": 10, "百": 100, "千": 1000}
ART_RE = re.compile(rf"^第([{CN}]+)条[\s\u3000]")

TAG_RE = re.compile(
    r'<div class="f-article-title-large[^"]*">(?P<big>.*?)</div>'
    r'|<div class="f-article-title-small[^"]*">(?P<chap>.*?)</div>'
    r'|<p class="f-article-title-tiny">(?P<art>.*?)</p>'
    r'|<p>(?P<para>.*?)</p>', re.S)


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


def strip_tags(s: str) -> str:
    s = re.sub(r"<[^>]+>", "", s)
    s = s.replace("&nbsp;", " ").replace("&amp;", "&")
    return re.sub(r"[\r\n\t]+", " ", s).strip()


def fetch() -> str:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    req = urllib.request.Request(URL, headers=UA)
    with urllib.request.urlopen(req, timeout=30, context=ctx) as r:
        raw = r.read()
    m = re.search(rb'charset=["\']?([\w-]+)', raw[:4000])
    enc = m.group(1).decode("ascii", "ignore") if m else "utf-8"
    for e in (enc, "utf-8", "gb18030"):
        try:
            return raw.decode(e)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", "ignore")


def parse(html: str) -> list[str]:
    # 目录是 <div class="catalog_text">，和正文不冲突（本正则根本不匹配 div），
    # 但目录里没有 <p>，所以不必额外切分。真正的正文从「总　则」那行开始。
    out: list[str] = []
    for m in TAG_RE.finditer(html):
        if m.group("big") is not None:
            t = strip_tags(m.group("big")).replace("\u3000", "")
            if t:
                out.append(t)                      # 总则 / 分则 / 附则
        elif m.group("chap") is not None:
            t = strip_tags(m.group("chap"))
            if t:
                out.append(t)                      # 第X章　XXX
        elif m.group("art") is not None:
            out.append(strip_tags(m.group("art")))  # 第X条　正文
        elif m.group("para") is not None:
            t = strip_tags(m.group("para"))
            if not t:
                continue
            # 续款接在上一条后面；若上一条不是条文（页面噪音），丢掉
            if out and ART_RE.match(out[-1]):
                out[-1] = out[-1] + "\n" + t
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()

    lines = parse(fetch())
    arts = [l for l in lines if ART_RE.match(l)]
    print(f"抽到条文 {len(arts)} 条")

    seen: dict[int, str] = {}
    order: list[str] = []
    for ln in lines:
        m = ART_RE.match(ln)
        if not m:
            order.append(ln)
            continue
        n = cn2int(m.group(1))
        if n in seen:
            print(f"[warn] 第 {n} 条重复，保留先出现的")
            continue
        seen[n] = ln
        order.append(ln)

    missing = [i for i in range(1, 429) if i not in seen]
    print(f"共 {len(seen)} 条，缺 {len(missing)} 条")
    if missing:
        print(f"  缺失：{missing[:30]}{' ...' if len(missing) > 30 else ''}")
    print(f"条号范围：{min(seen)} – {max(seen)}")

    probes = {52: "无效", 114: "违约金", 119: "减损", 428: "同时废止"}
    print("\n抽查：")
    for n, kw in probes.items():
        t = seen.get(n, "")
        print(f"  {'✔' if kw in t else '✘'} 第{n}条 含「{kw}」 {len(t)} 字  {t[:40]}")

    if missing:
        print("\n[abort] 有缺失，不写文件")
        return 1
    if args.check:
        print("\n[check] 校验通过（未写文件）")
        return 0

    header = (
        "中华人民共和国合同法\n\n"
        "（1999年3月15日第九届全国人民代表大会第二次会议通过　1999年3月15日"
        "中华人民共和国主席令第十五号公布　自1999年10月1日起施行）\n\n"
    )
    OUT.write_text(header + "\n".join(order) + "\n", encoding="utf-8")
    print(f"\n已写入 {OUT}（{OUT.stat().st_size / 1024:.0f} KB）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
