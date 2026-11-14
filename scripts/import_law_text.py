# -*- coding: utf-8 -*-
"""把下载下来的法律全文（txt / html / docx）切成结构化条文，写成 JSONL。

为什么需要它：
  官方数据库的接口在沙箱网络里访问不到，而且法律文本**不能靠模型转述**——
  差一个「不」字，结论就反过来。所以走「人工下载原文 + 程序切分」这条路：
  文本一个字都不改，只做切分和编号，可核验。

切分的难点不在正则，在**条文内部的交叉引用**：
  「依照本法第四十七条规定…」这种句子里的「第四十七条」不是新条文的开头，
  直接按「第X条」切会切出一堆碎块。
  解法是只认**条号连续递增**的那些位置——第 N 条之后的下一个切点必须是
  第 N+1 条，引用句里的条号因为序号跳跃会被自动跳过。

用法：
    # 1) 把下载的文件放进 data/raw/
    # 2) 在 data/raw/manifest.json 里登记元信息
    # 3) 运行
    .venv\\Scripts\\python.exe scripts/import_law_text.py

manifest.json 格式：
[
  {"file": "劳动合同法.txt",
   "name": "中华人民共和国劳动合同法",
   "short": "劳动合同法",
   "level": 1,
   "effective": "2008-01-01",
   "status": "现行有效",
   "superseded_by": ""}
]
"""
from __future__ import annotations

import argparse
import html
import json
import re
import shutil
import sys
import zipfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw"
OUT = ROOT / "data" / "statutes.jsonl"

CN = "零一二三四五六七八九十百千两"
_CN_DIGIT = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
             "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_CN_UNIT = {"十": 10, "百": 100, "千": 1000}


def cn2int(s: str) -> int:
    """中文数字转整数。支持「四十七」「一百零八」「一千二百六十」这类写法。"""
    s = s.strip()
    if s.isdigit():
        return int(s)
    total, section, num = 0, 0, 0
    for ch in s:
        if ch in _CN_DIGIT:
            num = _CN_DIGIT[ch]
        elif ch in _CN_UNIT:
            unit = _CN_UNIT[ch]
            if unit == 10 and num == 0:
                num = 1                    # 「十七」= 17
            section += num * unit
            num = 0
        else:
            raise ValueError(f"非法中文数字：{s}")
    return total + section + num


def int2cn(n: int) -> str:
    """整数转中文条号，用于 article_no_cn 字段（展示与引用）。"""
    if n <= 0:
        return "零"
    digits = "零一二三四五六七八九"
    units = ["", "十", "百", "千"]
    parts: list[str] = []
    s = str(n)
    for i, ch in enumerate(s):
        d = int(ch)
        unit = units[len(s) - i - 1]
        if d == 0:
            if parts and parts[-1] != "零":
                parts.append("零")
            continue
        parts.append(digits[d] + unit)
    out = "".join(parts).rstrip("零")
    return out.replace("一十", "十") if out.startswith("一十") else out


# ---------------------------------------------------------------- 读文件

def read_text(path: Path) -> str:
    ext = path.suffix.lower()
    if ext == ".docx":
        return _read_docx(path)
    raw = path.read_bytes()
    for enc in ("utf-8", "utf-8-sig", "gb18030", "gbk"):
        try:
            txt = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise SystemExit(f"{path.name} 编码无法识别")

    if ext in (".html", ".htm"):
        txt = _strip_html(txt)
    return txt


def _read_docx(path: Path) -> str:
    """docx 就是一堆 XML 打的 zip，取 word/document.xml 里的文本即可。"""
    with zipfile.ZipFile(path) as z:
        xml = z.read("word/document.xml").decode("utf-8", "ignore")
    xml = re.sub(r"</w:p>", "\n", xml)              # 段落分隔
    xml = re.sub(r"<w:tab[^>]*/>", "\t", xml)
    xml = re.sub(r"<[^>]+>", "", xml)
    return html.unescape(xml)


def _strip_html(txt: str) -> str:
    txt = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", "", txt)
    # 块级标签转换行，行内标签直接去掉，避免把一句话切碎
    txt = re.sub(r"(?i)</?(p|div|br|tr|li|h\d)[^>]*>", "\n", txt)
    txt = re.sub(r"<[^>]+>", "", txt)
    txt = html.unescape(txt)
    # 全角空格、制表符统一成空格
    txt = txt.replace("\u3000", " ").replace("\xa0", " ")
    lines = [ln.strip() for ln in txt.splitlines()]
    return "\n".join(ln for ln in lines if ln)


# ---------------------------------------------------------------- 切分

_ART_RE = re.compile(rf"第([{CN}\d]+)条")
_CHAP_RE = re.compile(rf"第([{CN}\d]+)章\s*([^\n]{{0,30}})")
_SECT_RE = re.compile(rf"第([{CN}\d]+)节\s*([^\n]{{0,30}})")


def split_articles(text: str) -> tuple[list[dict], list[str]]:
    """切出条文列表，返回 (条文, 警告)。

    只认条号连续递增的切点：正文里「依照本法第四十七条」这类交叉引用
    会让序号跳跃，被自动排除。这是本脚本能正确处理长文本的关键。
    """
    warns: list[str] = []
    cands: list[tuple[int, int, int]] = []          # (位置, 条号, 原始文本长度)
    for m in _ART_RE.finditer(text):
        try:
            n = cn2int(m.group(1))
        except (ValueError, KeyError):
            continue
        if 1 <= n <= 2000:
            cands.append((m.start(), n, m.end() - m.start()))

    chosen: list[tuple[int, int]] = []              # (位置, 条号)
    expect = 1
    for pos, num, _ in cands:
        if num == expect:
            chosen.append((pos, num))
            expect += 1

    if not chosen:
        return [], [f"没切出任何条文（找到 {len(cands)} 个候选）"]
    if chosen[0][1] != 1:
        warns.append("未找到「第一条」，文件开头可能缺内容")
    if len(chosen) < 3:
        warns.append(f"只切出 {len(chosen)} 条，大概率切分失败，检查原文格式")

    # 章节位置表，用于给每条打 chapter_path
    marks: list[tuple[int, str]] = []
    chapter = ""
    for m in _CHAP_RE.finditer(text):
        chapter = f"第{m.group(1)}章 {m.group(2).strip()}".strip()
        marks.append((m.start(), chapter))
    for m in _SECT_RE.finditer(text):
        if chapter:
            marks.append((m.start(), f"{chapter} 第{m.group(1)}节 {m.group(2).strip()}"))

    arts: list[dict] = []
    for i, (pos, num) in enumerate(chosen):
        end = chosen[i + 1][0] if i + 1 < len(chosen) else len(text)
        body = text[pos:end]
        # 去掉条号本身，剩下的就是正文
        body = _ART_RE.sub("", body, count=1).strip()
        # 章/节标题常落在上一段末尾（「…依照第四十七条处理。\n\n第二章 具体规定」），
        # 不清掉就会混进上一条的正文，检索时污染证据。
        body = re.sub(rf"(?m)^\s*第[{CN}\d]+[章节]\s*[^\n]*$", "", body)
        body = re.sub(rf"(?m)^\s*目\s*录\s*$", "", body)
        # 尾部常见噪声：页面导航、上一页下一页
        body = re.split(r"(上一篇|下一篇|返回顶部|打印本页|关闭窗口)", body)[0].strip()
        body = re.sub(r"[ \t]+", " ", body)
        body = re.sub(r"\n{3,}", "\n\n", body).strip()
        path = ""
        for mpos, label in marks:
            if mpos <= pos:
                path = label
            else:
                break
        arts.append({"article_no": num, "chapter_path": path, "content": body})
    return arts, warns


# ---------------------------------------------------------------- 主流程

def _repealed_note(entry: dict, article_no: int) -> str:
    """取该条的「部分废止」说明。没有就返回空串。

    manifest 里 repealed_articles 形如：
      [{"article_no": 32, "paragraph": 1,
        "repealed_by": "劳动争议解释二", "repealed_effective": "2025-09-01",
        "repealed_note": "第一款自 2025-09-01 起被…废止"}]
    只在条号对得上时返回，供 check_validity 与生成提示词使用。
    """
    for r in entry.get("repealed_articles") or []:
        if int(r.get("article_no", 0)) == int(article_no):
            txt = str(r.get("repealed_note", "")).strip()
            if txt:
                return txt
            para = r.get("paragraph")
            by = r.get("repealed_by") or "后法"
            date = r.get("repealed_effective") or ""
            which = f"第{para}款" if para else "本条"
            return f"{which}自 {date} 起已被《{by}》废止"
    return ""

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw", default=str(RAW), help="原文目录")
    ap.add_argument("--out", default=str(OUT), help="输出 JSONL")
    ap.add_argument("--keep", action="store_true", help="与已有 JSONL 合并而非覆盖")
    ap.add_argument("--force", action="store_true",
                    help="即使新结果比现有少也覆盖（覆盖前先把旧文件备份）")
    args = ap.parse_args()

    raw_dir = Path(args.raw)
    if not raw_dir.exists():
        print(f"目录不存在：{raw_dir}")
        print("请先建目录，把下载的法律全文放进去，再写 manifest.json")
        return 1

    mf = raw_dir / "manifest.json"
    if not mf.exists():
        print(f"缺少 {mf}")
        print("""
示例：
[
  {"file": "劳动合同法.txt",
   "name": "中华人民共和国劳动合同法",
   "short": "劳动合同法",
   "level": 1,
   "effective": "2008-01-01",
   "status": "现行有效"}
]
""")
        return 1

    entries = json.loads(mf.read_text(encoding="utf-8"))
    out_path = Path(args.out)
    existing: list[dict] = []
    if args.keep and out_path.exists():
        existing = [json.loads(l) for l in
                    out_path.read_text(encoding="utf-8").splitlines() if l.strip()]

    rows: list[dict] = []
    for e in entries:
        f = raw_dir / e["file"]
        if not f.exists():
            print(f"!! 文件缺失：{f.name}")
            continue
        text = read_text(f)
        arts, warns = split_articles(text)
        print(f"{e['short']:<16}{len(arts):>4} 条  ({len(text):,} 字符)  {f.name}")
        for w in warns:
            print(f"   ⚠ {w}")
        if not arts:
            continue
        for a in arts:
            note = _repealed_note(e, a["article_no"])
            law_status = e.get("status", "现行有效")
            # 条级时效：整部法已废止 → 每条都废止；整部法只是「部分失效」
            # （因为某一款被后法干掉）→ 只有那一条标部分失效，其余仍是现行有效。
            # 一律继承法律级状态会让 54 条全部挂上「部分失效」，误导模型。
            if law_status == "已废止":
                art_status = "已废止"
            elif note:
                art_status = "部分失效"
            else:
                art_status = "现行有效"
            rows.append({
                "id": f"{e['short']}_{a['article_no']}",
                "law_name": e["name"],
                "law_short": e["short"],
                "article_no": str(a["article_no"]),   # 必须是字符串：
                # Evidence.article_no 是 str 类型，写成 int 会让所有工具在
                # 转证据时抛 ValidationError，表现成「工具执行出错、证据 0 条」
                "article_no_cn": f"第{int2cn(a['article_no'])}条",
                "chapter_path": a["chapter_path"],
                "content": a["content"],
                "status": art_status,
                "effective_date": e.get("effective", ""),
                "superseded_by": e.get("superseded_by") or None,
                # 整部法有效、但某一条（某一款）已被后法废止的情形。
                # 这是「版本感知」最细的一层，也是最能体现法律检索功力的地方：
                # 解释（一）整体仍现行有效，唯独第三十二条第一款已经死了。
                "repealed_note": note,
                "hierarchy_level": e.get("level", 1),
                "source_url": e.get("source_url", "https://flk.npc.gov.cn/"),
            })

    if not rows:
        print("没有产出任何条文，检查原文格式")
        return 1

    if existing:
        seen = {(r["law_short"], r["article_no"], r.get("status")) for r in existing}
        rows = [r for r in rows
                if (r["law_short"], r["article_no"], r.get("status")) not in seen] + existing

    # 按条号的数值排，不能按字符串，否则「第十条」会排到「第二条」前面
    rows.sort(key=lambda r: (r["law_short"], int(r["article_no"])))

    # ---- 覆盖保护 ----
    # 切出来的比现存的少，默认不写。少的那部分通常**不是**「原文没抄全」，
    # 而是从来没进过 raw/：statutes.jsonl 里的「工伤保险条例」6 条
    # （第 17/20/33/37/62/64 条）是单独补录的，data/raw/ 里没有它的原文。
    # 照默认路径覆盖，这 6 条会无声消失——而期限计算、工伤保险定性规则
    # 正依赖它们，退化后的表现是「本库未收录」，看起来像检索坏了。
    if out_path.exists() and not args.keep:
        old_rows = [json.loads(l) for l in
                    out_path.read_text(encoding="utf-8").splitlines() if l.strip()]
        if len(rows) < len(old_rows):
            gone = sorted({r["law_short"] for r in old_rows} -
                          {r["law_short"] for r in rows})
            print("-" * 60)
            print(f"!! 拒绝覆盖：现有 {len(old_rows)} 条，这次只切出 {len(rows)} 条，"
                  f"少 {len(old_rows) - len(rows)} 条。")
            if gone:
                print(f"   少掉的是：{'、'.join(gone)} —— 它们的原文不在 {raw_dir} 里。")
            print("   这不是「原文没抄全」，是这些条文本来就没进过 raw/。")
            if not args.force:
                print("   确认要覆盖（真的会少掉上面那些）加 --force。")
                return 2
            backup = out_path.with_name(
                f"{out_path.name}.bak-{datetime.now():%Y%m%d-%H%M%S}")
            shutil.copy2(out_path, backup)
            print(f"   --force：旧文件已备份为 {backup.name}")

    out_path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
        encoding="utf-8")

    laws = sorted({r["law_short"] for r in rows})
    print("-" * 60)
    print(f"写出 {len(rows)} 条 → {out_path}")
    print(f"覆盖 {len(laws)} 部法：{'、'.join(laws)}")
    empty = [r for r in rows if len(r["content"]) < 8]
    if empty:
        print(f"⚠ {len(empty)} 条正文异常短，建议抽查：")
        for r in empty[:5]:
            print(f"   {r['id']}  {r['content'][:40]!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
