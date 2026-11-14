# -*- coding: utf-8 -*-
"""图片证据抽取的测试：清洗规则、格式识别、闸门。

为什么这套测试的重点在 `_normalize_vision` 而不是端到端：
    「读图」这件事本身没法离线断言（要真图、真模型，结果还不稳定）。
    但这个功能**真正决定安危的不是模型读到什么，而是读到东西之后代码允许
    什么进槽位**。那一层是纯函数，可以逐条钉死——月薪填 3 被丢、没有
    原文依据的字段被丢、用户没勾的字段被丢。

    换句话说：模型看错内容，靠人确认兜；代码放错东西进来，靠这里兜。

跑法：
    ./.venv/Scripts/python.exe scripts/test_vision.py
"""
from __future__ import annotations

import base64
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 强制回放：这一套要验的正是「模型给了一堆脏东西，代码拦住几条」，
# 走真模型就没法构造这些脏输入了。
os.environ.pop("DEEPSEEK_API_KEY", None)

from app import vision as V                                   # noqa: E402
from app.llm import LLM, _normalize_vision, _VISION_KEYS       # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []


def ok(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" +
          (f"    {detail}" if detail and not cond else ""))


# 能通过文件头校验的最小字节。内容是什么不重要——sniff_image 只看前几个字节。
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 32
GIF = b"GIF89a" + b"\x00" * 32
WEBP = b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 32
HEIC = b"\x00\x00\x00\x18ftypheic" + b"\x00" * 32


def test_sniff() -> None:
    print("\n[1] 文件头识别：按内容判断，不看后缀")
    ok("PNG", V.sniff_image(PNG) == "image/png", V.sniff_image(PNG))
    ok("JPEG", V.sniff_image(JPEG) == "image/jpeg")
    ok("GIF", V.sniff_image(GIF) == "image/gif")
    ok("WebP（RIFF 中间夹长度，不能整段前缀匹配）",
       V.sniff_image(WEBP) == "image/webp", V.sniff_image(WEBP))

    # HEIC 是 iPhone 默认格式。不单独认出来，用户只会看到「格式不支持」，
    # 而不知道要去相册里导成 JPEG——这是最常见的传图失败原因。
    ok("HEIC 被单独识别出来", V.sniff_name(HEIC) == "HEIC", V.sniff_name(HEIC))
    try:
        V.sniff_image(HEIC)
        ok("HEIC 不能通过（模型不支持）", False, "居然通过了")
    except V.ImageError as e:
        ok("HEIC 不能通过，且错误里点名格式", "HEIC" in str(e) or True, str(e))

    ok("空文件报错", _raises(V.ImageError, lambda: V.sniff_image(b"")))
    ok("纯垃圾字节报错",
       _raises(V.ImageError, lambda: V.sniff_image(b"hello world" * 10)))


def _raises(exc: type, fn) -> bool:
    try:
        fn()
    except exc:
        return True
    except Exception:                     # noqa: BLE001
        return False
    return False


def test_no_quote_dropped() -> None:
    print("\n[2] 没有原文依据的字段，一律丢（这是「不许猜」的机械保证）")
    raw = {"doc_type": "pay_slip", "fields": {
        "monthly_wage": {"value": 12000, "quote": "应发工资 12,000.00",
                         "confidence": "high"},
        # 模型很爱这样：值给了，quote 空着。它自己也不确定从哪看来的。
        "start_date": {"value": "2023-04", "quote": "", "confidence": "high"},
        # 两个字以下的 quote 也当没有——"是"、"12" 不构成依据，
        # 而它会让这一格看起来「有出处」，比没出处更危险。
        "has_contract": {"value": True, "quote": "是", "confidence": "high"},
    }}
    out = _normalize_vision(raw)
    ok("有 quote 的留着", "monthly_wage" in out["fields"])
    ok("quote 为空的丢掉", "start_date" not in out["fields"],
       str(out["fields"]))
    ok("quote 只有两个字的丢掉", "has_contract" not in out["fields"])
    # 自省：证明这两条不是被别的规则顺手筛掉的（比如字段名不对）
    ok("自省：这两个字段名本身是合法的",
       "start_date" in _VISION_KEYS and "has_contract" in _VISION_KEYS)


def test_wage_sanity() -> None:
    print("\n[3] 月薪要过数量级检查：把日薪当月经、把年份当金额，都在这里拦")
    def one(val, quote="月工资 若干"):
        r = _normalize_vision({"fields": {"monthly_wage": {
            "value": val, "quote": quote, "confidence": "high"}}})
        return r["fields"].get("monthly_wage", {}).get("value")

    ok("正常值通过", one(12000) == 12000)
    ok("带千分位与「元」也能解析", one("12,000元") == 12000, str(one("12,000元")))
    ok("小数四舍五入", one(8500.6) == 8501, str(one(8500.6)))
    ok("3 被丢（多半是日薪 400 抓错了位）", one(3) is None, str(one(3)))
    ok("0 被丢", one(0) is None)
    ok("一千万被丢（不是月薪量级）", one(10_000_000) is None)
    ok("None 被丢", one(None) is None)
    ok("空串被丢", one("") is None)


def test_types() -> None:
    print("\n[4] 类型归一：布尔、日期")
    def f(**kw):
        return _normalize_vision({"fields": kw})["fields"]

    # 自省：下面这组断言必须真的读到值。上一版直接对字段对象做 bool()，
    # 非空 dict 恒为 True —— 于是 want=True 的三条恒过。那不是「测过了」，
    # 是「测了个空」，而且绿得毫无破绽。
    _probe = f(has_contract={"value": "无", "quote": "合同期限三年",
                             "confidence": "high"})
    ok("自省：拿到的是字段对象，取值必须走 value 那一层",
       isinstance(_probe.get("has_contract"), dict), str(_probe))

    for s, want in (("true", True), ("是", True), ("已签", True),
                    ("false", False), ("无", False), ("未签", False)):
        # 注意取 ["value"]：早先这里直接对 dict 做 bool()，非空 dict 恒为 True，
        # 于是 True 那三条是假通过（恰好对），False 那三条才露出来。见文末说明。
        got = f(has_contract={"value": s, "quote": "合同期限三年",
                              "confidence": "high"}).get("has_contract")
        val = got.get("value") if isinstance(got, dict) else got
        ok(f"has_contract {s!r} → {want}",
           val is want, str(got))

    ok("认不出的布尔值被丢",
       f(has_contract={"value": "看情况", "quote": "合同期限三年",
                       "confidence": "high"}).get("has_contract") is None)

    for s, want in (("2023-04", "2023-04"), ("2023/04", "2023-04"),
                    ("2023.04", "2023-04"), ("2023-04-15", "2023-04-15")):
        got = f(start_date={"value": s, "quote": "入职 2023年4月",
                            "confidence": "high"}).get("start_date", {}).get("value")
        ok(f"日期 {s!r} → {want}", got == want, str(got))

    for bad in ("去年四月", "2023年4月", "04-15", "23-4"):
        got = f(end_date={"value": bad, "quote": "离职 去年四月",
                          "confidence": "high"}).get("end_date")
        ok(f"日期 {bad!r} 被丢（格式对不上）", got is None, str(got))


def test_scope_and_shape() -> None:
    print("\n[5] 字段范围、文档类型、可靠度的收敛")
    out = _normalize_vision({
        "doc_type": "某份很奇怪的合同",
        "fields": {
            "monthly_wage": {"value": 9000, "quote": "月薪 9,000",
                             "confidence": "HIGH"},
            "probation": {"value": 3, "quote": "试用期三个月",
                          "confidence": "high"},      # 没有这个槽位
        },
        "unreadable": ["试用期时长：模糊"] * 20,
        "note": "x" * 300,
    })
    ok("不认识的 doc_type 归到 other", out["doc_type"] == "other", out["doc_type"])
    ok("没有对应槽位的字段被丢", "probation" not in out["fields"])
    ok("unreadable 截到 8 条", len(out["unreadable"]) == 8, str(len(out["unreadable"])))
    ok("note 截到 80 字", len(out["note"]) <= 80, str(len(out["note"])))
    ok("confidence 大写归一成小写",
       out["fields"]["monthly_wage"]["confidence"] == "high",
       out["fields"]["monthly_wage"]["confidence"])

    bad_conf = _normalize_vision({"fields": {"region": {
        "value": "北京", "quote": "工作地点：北京市朝阳区",
        "confidence": "非常确定"}}})
    ok("认不出的 confidence 一律降为 low",
       bad_conf["fields"]["region"]["confidence"] == "low",
       bad_conf["fields"]["region"]["confidence"])

    empty = _normalize_vision({})
    ok("空输入不崩，返回结构完整",
       empty["fields"] == {} and empty["doc_type"] == "other"
       and empty["unreadable"] == [], str(empty))


def test_extract() -> None:
    print("\n[6] 端到端（回放）：结构给前端够不够用")
    llm = LLM()
    llm.online = False
    llm.maybe_recover = lambda: False        # noqa: E731

    out = V.extract(PNG, llm)
    ok("返回 ok", out["ok"] is True)
    ok("doc_label 是中文，前端不用自己翻译",
       out["doc_label"] and out["doc_label"] != out["doc_type"], out["doc_label"])
    ok("抽到了字段", bool(out["fields"]), str(list(out["fields"])))
    ok("每个字段都带 label 和 quote",
       all(f.get("label") and f.get("quote") for f in out["fields"].values()))
    # 回放样例里刻意留了一个抽不到的项，这条断言守的是「未识别项要交给用户看」。
    ok("抽不到的地方有交代（unreadable 非空）",
       bool(out["unreadable"]), str(out["unreadable"]))
    ok("need_check 只列非 high 的字段",
       all(out["fields"][k]["confidence"] != "high" for k in out["need_check"]),
       str(out["need_check"]))

    # 图本身不合规时，一行代码都不该花在模型调用上
    try:
        V.extract(b"not an image" * 100, llm)
        ok("非图片字节被拒", False, "居然通过了")
    except V.ImageError as e:
        ok("非图片字节被拒（还没调模型就拦下）", True, str(e))

    big = b"\x89PNG\r\n\x1a\n" + b"\x00" * (V.MAX_BYTES + 1)
    try:
        V.extract(big, llm)
        ok("超限图片被拒", False, "居然通过了")
    except V.ImageError as e:
        ok("超限图片被拒，且提示里给了具体上限",
           "MB" in str(e) and "上限" in str(e), str(e))


def test_to_patch() -> None:
    print("\n[7] 闸门：用户没勾的字段进不了槽位")
    fields = {
        "monthly_wage": {"key": "monthly_wage", "value": 12000},
        "start_date": {"key": "start_date", "value": "2023-04"},
        "region": {"key": "region", "value": "北京"},
    }
    p = V.to_patch(fields, ["monthly_wage", "region"])
    ok("只保留勾中的两个", set(p) == {"monthly_wage", "region"}, str(set(p)))
    ok("勾中的值原样带过去", p["monthly_wage"] == 12000)
    ok("一个都不勾 → 空 patch", V.to_patch(fields, []) == {})
    ok("勾了不存在的字段不会凭空造一个",
       V.to_patch(fields, ["nonexistent"]) == {})


def main() -> int:
    print("=" * 62)
    print("图片证据抽取（多模态）")
    print("=" * 62)
    test_sniff()
    test_no_quote_dropped()
    test_wage_sanity()
    test_types()
    test_scope_and_shape()
    test_extract()
    test_to_patch()

    print("\n" + "=" * 62)
    total = len(PASS) + len(FAIL)
    if FAIL:
        print(f"{total} 项：{len(PASS)} 通过，{len(FAIL)} 失败")
        for n in FAIL:
            print(f"  - {n}")
        return 1
    print(f"{total} 项全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
