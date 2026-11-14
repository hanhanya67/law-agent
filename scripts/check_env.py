#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""说明与实现对账：`.env.example` 的键、`交付说明.txt` 里的数字。

为什么需要它：这些文件是用户唯一会读的东西，而它们跟代码/数据之间没有任何约束。
三个方向出问题都不会报错：

  清单里有、代码不读  → 用户认真填了，程序根本不看。表现是「按文档配了没反应」，
                        比报错难查得多，因为用户会怀疑是自己写错格式。
  代码读、清单没有    → 这个开关等于不存在。没人会去翻源码找能调什么。
  说明里的数字是旧的  → 收货方拿它当验收基准，对不上就以为东西坏了。

都只能靠人记得同步，所以放在这里对账。

另外查一件小事：清单里给整数键写的默认值，是不是真的能 int()。
`MAX_AGENT_STEPS=8次` 这种，用户照抄进去，启动时才崩在 int()。
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV = ROOT / ".env.example"

# 真正的声明行：行首就是 KEY=
DECL = re.compile(r"^([A-Z][A-Z0-9_]*)=", re.M)
# 「提到过」：允许 `# KEY=` 这种注释形式（表示存在但默认不启用的键）
MENTION = re.compile(r"^#?[ \t]*([A-Z][A-Z0-9_]*)=", re.M)
READ_PATTERNS = [
    r'os\.getenv\(\s*["\']([A-Z][A-Z0-9_]*)["\']',
    r'os\.environ\.get\(\s*["\']([A-Z][A-Z0-9_]*)["\']',
    r'os\.environ\[\s*["\']([A-Z][A-Z0-9_]*)["\']\s*\]',
]
INT_READ = r'int\(\s*os\.getenv\(\s*["\']([A-Z][A-Z0-9_]*)["\']'

# 允许「代码读、清单不必提」的键。空集合是目标状态——留这个常量是为了
# 以后真要豁免时有个明确的地方写理由，而不是散在断言里。
EXEMPT_HIDDEN: set[str] = set()

fails: list[str] = []
n_pass = 0


def check(name: str, ok: bool, detail: object = "") -> None:
    global n_pass
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}"
          + (f"  → {detail}" if detail != "" else ""))
    if ok:
        n_pass += 1
    else:
        fails.append(name)


def collect_code() -> str:
    me = Path(__file__).resolve()
    out = []
    for d in ("app", "scripts"):
        for p in (ROOT / d).rglob("*.py"):
            if "__pycache__" in p.parts:
                continue
            # 跳过自己：本文件的 self_test() 里写着 os.getenv("A_KEY") 这类
            # 夹具字符串，不跳过就会被自己的正则扫成「代码读某个键」。
            if p.resolve() == me:
                continue
            out.append(p.read_text(encoding="utf-8", errors="replace"))
    return "\n".join(out)


def audit(env_text: str, code_text: str) -> dict:
    declared = set(DECL.findall(env_text))
    mentioned = set(MENTION.findall(env_text))
    read: set[str] = set()
    for pat in READ_PATTERNS:
        read |= set(re.findall(pat, code_text))

    dup = [k for k in sorted(declared)
           if len(re.findall(rf"^{re.escape(k)}=", env_text, re.M)) > 1]

    int_keys = set(re.findall(INT_READ, code_text))
    bad_int = []
    for m in re.finditer(r"^([A-Z][A-Z0-9_]*)=([^\n]*)$", env_text, re.M):
        k, v = m.group(1), m.group(2).strip()
        if k in int_keys and v:
            try:
                int(v)
            except ValueError:
                bad_int.append((k, v))

    return {
        "phantom": sorted(declared - read),
        "hidden": sorted(read - mentioned - EXEMPT_HIDDEN),
        "dup": dup,
        "bad_int": bad_int,
        "n_declared": len(declared),
        "n_read": len(read),
    }


def self_test() -> None:
    """先证明这个对账会红。它失灵的样子是「两个方向都空」，看着最正常。"""
    env = "A_KEY=1\nB_KEY=2\n"
    code = 'os.getenv("A_KEY")\nos.getenv("C_KEY")\n'
    r = audit(env, code)
    check("自检：清单有、代码不读能抓到", r["phantom"] == ["B_KEY"], r["phantom"])
    check("自检：代码读、清单没提能抓到", r["hidden"] == ["C_KEY"], r["hidden"])
    bad = audit("N=abc\n", 'int(os.getenv("N", "1"))\n')
    check("自检：整数键写成非整数能抓到", bad["bad_int"] == [("N", "abc")], bad["bad_int"])


def corpus_rows() -> int:
    p = ROOT / "data" / "statutes.jsonl"
    if not p.exists():
        return 0
    return sum(1 for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip())


def suite_count() -> int:
    """数 run_all 里登记的套件数。用 ast 只解析不执行。"""
    try:
        tree = ast.parse((ROOT / "scripts" / "run_all.py")
                         .read_text(encoding="utf-8"))
        n = 0
        for node in tree.body:
            if isinstance(node, ast.Assign) and \
                    getattr(node.targets[0], "id", "") in ("STEPS", "WITH_SERVICE"):
                n += len(ast.literal_eval(node.value))
        return n
    except (OSError, SyntaxError, ValueError):
        return -1


def verified_texts() -> dict[str, str]:
    """取出 `pack_src.py` 里两段「验证过什么」文案。

    用 ast 只解析不执行——直接 import 会连带跑一遍 ROOT、排除清单那些模块级
    语句，没必要。要的是那两个字符串常量本身。

    为什么值得单查：这两段是**收货方照着验收**的地方（比 交付说明 还靠前，
    它直接贴在包根目录的「打包说明.txt」里）。改 run_all 的套件数时，
    交付说明 被门禁挡下来了，这两段却没人看，于是留在「22 / 22 通过」——
    收货方跑出 24 个，第一反应是「包是不是被人动过」，而不是「文案过期了」。
    """
    p = ROOT / "scripts" / "pack_src.py"
    if not p.exists():
        return {}
    try:
        tree = ast.parse(p.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return {}
    out: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            name = getattr(node.targets[0], "id", "")
            if name in ("VERIFIED", "VERIFIED_WITH_KEY"):
                try:
                    out[name] = ast.literal_eval(node.value)
                except ValueError:
                    pass
    return out


def check_doc_numbers() -> None:
    """交付说明里的数字。它是收货方最先读的中文文档，比 README 更容易被当成准。"""
    doc = ROOT / "交付说明.txt"
    if not doc.exists():
        check("交付说明.txt 存在", False, "缺文件")
        return
    text = doc.read_text(encoding="utf-8")

    n_real = corpus_rows()
    m = re.search(r"（(\d+) 条条文）", text)
    check("交付说明：条文数与语料一致",
          bool(m) and int(m.group(1)) == n_real,
          f"文档写 {m.group(1) if m else '?'} / 实际 {n_real}")

    n_suite = suite_count()
    m2 = re.search(r"(\d+) 个套件", text)
    check("交付说明：套件数与回归登记一致",
          bool(m2) and int(m2.group(1)) == n_suite,
          f"文档写 {m2.group(1) if m2 else '?'} / 实际 {n_suite}")

    # 验收文案里的「N / N 通过」。两段都查：默认包和带 key 包各有一段，
    # 少查一段就等于少一半保护，而漏掉的恰好是最常被发出去的那个默认包。
    texts = verified_texts()
    check("pack_src：两段验收文案都在",
          set(texts) == {"VERIFIED", "VERIFIED_WITH_KEY"},
          sorted(texts))
    for name, t in texts.items():
        m3 = re.search(r"(\d+)\s*/\s*(\d+)\s*通过", t)
        check(f"pack_src：{name} 的套件数与回归登记一致",
              bool(m3) and m3.group(1) == m3.group(2) == str(n_suite),
              (f"文案写 {m3.group(1)}/{m3.group(2)} / 实际 {n_suite}"
               if m3 else "没找到「N / N 通过」"))
        # 「看的是 N 个通过数」只在默认包里出现，有就查、没有就跳过。
        m4 = re.search(r"看的是\s*(\d+)\s*个通过数", t)
        if m4:
            check(f"pack_src：{name} 的「看的是 N 个通过数」一致",
                  m4.group(1) == str(n_suite),
                  f"文案写 {m4.group(1)} / 实际 {n_suite}")


def check_key_script() -> None:
    """check_key.py 是演示前唯一的自检脚本，它自己的崩溃比它要查的问题更难查。

    2026-11-07 实测：模型是推理模型，思考与正文共享 max_tokens。流式那一项
    的预算给小了，一个正文分片都收不到，`first` 一直是 None，而报告那行
    `f"{first:.0f} ms"` 直接抛 TypeError: unsupported format string passed to
    NoneType.__format__——带 traceback、退出码 1。照着这个红去换 key 能折腾
    半天：**它把「这一项没测出东西」报成了「key 坏了」**。

    这里守的是那条岔路还在，不是守某种写法：只要有人把「没收到正文」的判断
    删掉、让它继续去格式化 None，这一条就红。
    """
    p = ROOT / "scripts" / "check_key.py"
    if not p.exists():
        check("check_key.py 存在", False, "缺文件")
        return
    src = p.read_text(encoding="utf-8")
    check("check_key：流式「一个正文分片都没收到」有单独岔路",
          "if first is None:" in src,
          "已删掉 None 分支：收到空流时会抛 TypeError，而不是报「未测出」")


def main() -> int:
    print("说明与实现对账（.env.example / 交付说明 ↔ 代码 / 数据）")
    if not ENV.exists():
        print(f"缺少 {ENV}")
        return 1

    self_test()

    env_text = ENV.read_text(encoding="utf-8")
    r = audit(env_text, collect_code())

    check("清单里没有「代码根本不读」的键", not r["phantom"],
          f"假开关：{r['phantom']}" if r["phantom"] else f"{r['n_declared']} 个键")
    check("代码读的键清单里都提到了", not r["hidden"],
          f"没提到：{r['hidden']}" if r["hidden"] else f"代码读 {r['n_read']} 个键")
    check("没有重复声明", not r["dup"], r["dup"] or "无重复")
    check("整数键的默认值能 int()", not r["bad_int"],
          r["bad_int"] or "全部可解析")

    check_doc_numbers()
    check_key_script()

    print()
    if fails:
        print(f"{len(fails)} 项失败：{'、'.join(fails)}")
        return 1
    # 数字由计数器给出，不写死。原来这行是「4 项对账 + 3 项自检 + 2 项文档数字」，
    # 每加一条检查就变成假话——而它恰恰是给人看「一共查了多少」的那句。
    print(f"全部通过（共 {n_pass} 项）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
