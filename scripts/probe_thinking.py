# -*- coding: utf-8 -*-
"""探测 deepseek-flash 的思考模式对真实 Agent 环节的影响。

为什么需要这个脚本：
  2026-10 的 deepseek-flash 是推理模型，默认 effort=high，会先输出
  reasoning_content 再输出 content，两者**共享 max_tokens 预算**。
  原代码里意图识别用的是 max_tokens=12，实测 content 恒为空串，
  finish_reason=length——所有问题都会掉进默认分支。

  这里测四种组合，给每个环节定出「要不要开思考 + 预算给多少」。
只用标准库，避免依赖没装完时跑不了。
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE = "https://api.deepseek.com/chat/completions"
MODEL = "deepseek-flash"


def _key() -> str:
    for ln in (Path(__file__).resolve().parent.parent / ".env").read_text(
            encoding="utf-8").splitlines():
        if ln.strip().startswith("DEEPSEEK_API_KEY="):
            return ln.split("=", 1)[1].strip()
    raise SystemExit("no key")


KEY = _key()


def call(payload: dict) -> dict:
    body = json.dumps(payload, ensure_ascii=False).encode()
    req = urllib.request.Request(
        BASE, data=body,
        headers={"Authorization": f"Bearer {KEY}",
                 "Content-Type": "application/json"})
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            data = json.loads(r.read())
    except urllib.error.HTTPError as e:
        return {"_err": f"HTTP {e.code}: {e.read().decode()[:300]}",
                "_ms": (time.perf_counter() - t0) * 1000}
    except Exception as e:                        # noqa: BLE001
        return {"_err": f"{type(e).__name__}: {e}",
                "_ms": (time.perf_counter() - t0) * 1000}
    data["_ms"] = (time.perf_counter() - t0) * 1000
    return data


def report(tag: str, d: dict, show: str = "content") -> None:
    ms = d.get("_ms", 0)
    if "_err" in d:
        print(f"  {tag:<26} {ms:>6.0f}ms  ERR {d['_err'][:160]}")
        return
    ch = d["choices"][0]
    msg = ch["message"]
    u = d.get("usage", {})
    rt = (u.get("completion_tokens_details") or {}).get("reasoning_tokens")
    calls = msg.get("tool_calls") or []
    out = ""
    if show == "tool" and calls:
        out = calls[0]["function"]["name"] + " " + calls[0]["function"]["arguments"]
    else:
        out = (msg.get("content") or "").replace("\n", " ")
    print(f"  {tag:<26} {ms:>6.0f}ms  finish={ch.get('finish_reason'):<6} "
          f"in={u.get('prompt_tokens', 0):<5} out={u.get('completion_tokens', 0):<4} "
          f"think={rt}  ->  {out[:70]!r}")


INTENT_SYS = ("你是意图分类器。只能输出以下之一，不要输出别的字符："
              "statute_lookup / case_consult / calc / clarify / out_of_scope")

TOOLS = [{
    "type": "function",
    "function": {
        "name": "search_statute",
        "description": "按关键词在法条库中检索，返回条文列表",
        "parameters": {"type": "object",
                       "properties": {"query": {"type": "string",
                                                "description": "法条术语检索词"}},
                       "required": ["query"]},
    },
}, {
    "type": "function",
    "function": {
        "name": "calculate_compensation",
        "description": "计算经济补偿或赔偿金，必须由工具算，不要自己心算",
        "parameters": {"type": "object",
                       "properties": {
                           "monthly_wage": {"type": "number"},
                           "start_date": {"type": "string"},
                           "end_date": {"type": "string"},
                           "leave_reason": {"type": "string"}},
                       "required": ["monthly_wage", "start_date"]},
    },
}]

DECISION_SYS = (
    "你是劳动法问答 Agent 的决策器。根据当前状态决定下一步：调用工具或直接作答。\n"
    "规则：条文事实必须靠 search_statute 检索，不得凭记忆回答；"
    "任何金额必须调用 calculate_compensation，不得心算。\n"
    "可用工具：search_statute(query)、calculate_compensation(...)\n"
    "已检索证据：（暂无）\n"
    "已知案情：月工资8000，2020-03入职，公司违法辞退\n"
    "预算：最多 8 步。")

GEN_SYS = (
    "你是劳动法律师助手。按【结论】【依据】【建议】三段作答，"
    "每条依据必须标注《法名》第N条，不得引用未提供的条文。\n\n"
    "可用条文：\n"
    "- 《中华人民共和国劳动合同法》第八十七条：用人单位违反本法规定解除或者终止"
    "劳动合同的，应当依照本法第四十七条规定的经济补偿标准的二倍向劳动者支付赔偿金。\n"
    "- 《中华人民共和国劳动合同法》第四十七条：经济补偿按劳动者在本单位工作的年限，"
    "每满一年支付一个月工资的标准向劳动者支付。六个月以上不满一年的，按一年计算；"
    "不满六个月的，向劳动者支付半个月工资的经济补偿。\n\n"
    "案情：月工资 8000 元，2020 年 3 月入职，2026 年 10 月被违法辞退。")


def main() -> None:
    print("=" * 100)
    print(f"模型 {MODEL} 思考模式实测")
    print("=" * 100)

    print("\n[1] 意图分类（当前代码 max_tokens=12）")
    for mt, think, tag in [
        (12, None, "mt=12 默认思考"),
        (12, {"type": "disabled"}, "mt=12 关思考"),
        (300, None, "mt=300 默认思考"),
        (300, {"type": "disabled"}, "mt=300 关思考"),
    ]:
        p = {"model": MODEL, "max_tokens": mt, "temperature": 0,
             "messages": [{"role": "system", "content": INTENT_SYS},
                          {"role": "user", "content": "公司拖欠我三个月工资，我能要回来吗"}]}
        if think:
            p["thinking"] = think
        report(tag, call(p))

    print("\n[2] 决策 + Function Calling（当前 max_tokens=800）")
    for mt, think, tag in [
        (800, None, "mt=800 默认思考"),
        (800, {"type": "disabled"}, "mt=800 关思考"),
    ]:
        p = {"model": MODEL, "max_tokens": mt, "temperature": 0,
             "tools": TOOLS, "tool_choice": "auto",
             "messages": [{"role": "system", "content": DECISION_SYS},
                          {"role": "user", "content": "用户问题：公司违法把我辞退了，我月薪8000，2020年3月入职，能拿多少赔偿金？"}]}
        if think:
            p["thinking"] = think
        report(tag, call(p), show="tool")

    print("\n[3] 答案生成（当前 max_tokens=2000）")
    for mt, think, tag in [
        (2000, None, "mt=2000 默认思考"),
        (2000, {"type": "disabled"}, "mt=2000 关思考"),
    ]:
        p = {"model": MODEL, "max_tokens": mt, "temperature": 0.3,
             "messages": [{"role": "system", "content": GEN_SYS},
                          {"role": "user", "content": "公司违法把我辞退了，我月薪8000，2020年3月入职，能拿多少赔偿金？"}]}
        if think:
            p["thinking"] = think
        report(tag, call(p))

    print("\n[4] 强制思考预算上限（budget_tokens）")
    for bt in (1024, 256):
        p = {"model": MODEL, "max_tokens": 2000, "temperature": 0,
             "thinking": {"type": "enabled", "budget_tokens": bt},
             "messages": [{"role": "system", "content": INTENT_SYS},
                          {"role": "user", "content": "违法解除劳动合同怎么赔偿"}]}
        report(f"budget={bt}", call(p))


if __name__ == "__main__":
    main()
