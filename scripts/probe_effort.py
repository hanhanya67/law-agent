# -*- coding: utf-8 -*-
"""补测：生成环节在 effort 三档 + 关闭思考下的质量/速度/成本权衡。

背景：默认 high 档生成一个答案要 7.5 秒、思考 954 token；
关掉思考只要 1.9 秒，但答案从 1208 token 缩到 216 token，明显变薄。
需要一个折中档位，所以分别测 low / high / max 和 disabled。
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

GEN_SYS = (
    "你是劳动法律师助手。按【结论】【依据】【建议】三段作答，"
    "每条依据必须标注《法名》第N条，不得引用未提供的条文。\n\n"
    "可用条文：\n"
    "- 《中华人民共和国劳动合同法》第八十七条：用人单位违反本法规定解除或者终止"
    "劳动合同的，应当依照本法第四十七条规定的经济补偿标准的二倍向劳动者支付赔偿金。\n"
    "- 《中华人民共和国劳动合同法》第四十七条：经济补偿按劳动者在本单位工作的年限，"
    "每满一年支付一个月工资的标准向劳动者支付。六个月以上不满一年的，按一年计算；"
    "不满六个月的，向劳动者支付半个月工资的经济补偿。\n"
    "- 《中华人民共和国劳动合同法》第四十八条：用人单位违反本法规定解除或者终止"
    "劳动合同，劳动者要求继续履行劳动合同的，用人单位应当继续履行；劳动者不要求"
    "继续履行劳动合同或者劳动合同已经不能继续履行的，用人单位应当依照本法第八十七"
    "条规定支付赔偿金。\n\n"
    "案情：月工资 8000 元，2020 年 3 月入职，2026 年 10 月被违法辞退。")


def call(payload: dict) -> dict:
    body = json.dumps(payload, ensure_ascii=False).encode()
    req = urllib.request.Request(
        BASE, data=body,
        headers={"Authorization": f"Bearer {KEY}",
                 "Content-Type": "application/json"})
    t0 = time.perf_counter()
    first = None
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            raw = r.read()
        data = json.loads(raw)
    except Exception as e:                        # noqa: BLE001
        return {"_err": f"{type(e).__name__}: {e}"}
    data["_ms"] = (time.perf_counter() - t0) * 1000
    return data


def run(tag: str, extra: dict) -> None:
    p = {"model": MODEL, "max_tokens": 4000, "temperature": 0.3,
         "messages": [{"role": "system", "content": GEN_SYS},
                      {"role": "user",
                       "content": "公司违法把我辞退了，我月薪8000，2020年3月入职，能拿多少赔偿金？"}]}
    p.update(extra)
    d = call(p)
    if "_err" in d:
        print(f"{tag:<18} ERR {d['_err'][:120]}")
        return
    ch = d["choices"][0]
    msg = ch["message"]
    u = d["usage"]
    rt = (u.get("completion_tokens_details") or {}).get("reasoning_tokens")
    body = msg.get("content") or ""
    has_sec = all(k in body for k in ("【结论】", "【依据】", "【建议】"))
    # 是否算对了：6 年 7 个月 → 7 个月工资 → 8000*7*2 = 112000
    right = "112,000" in body or "112000" in body or "112,000元" in body
    print(f"{tag:<18} {d['_ms']:>6.0f}ms  out={u.get('completion_tokens',0):<5} "
          f"think={rt!s:<5} 三段={'OK' if has_sec else 'NO'} 金额112000={'OK' if right else 'NO'} "
          f"字数={len(body)}")


print("=" * 92)
print("生成环节：思考档位权衡（max_tokens=4000）")
print("=" * 92)
run("disabled", {"thinking": {"type": "disabled"}})
run("effort=low", {"reasoning_effort": "low"})
run("effort=high", {"reasoning_effort": "high"})
run("effort=max", {"reasoning_effort": "max"})
run("默认(不传)", {})

print()
print("=" * 92)
print("生成样例（effort=low）")
print("=" * 92)
d = call({"model": MODEL, "max_tokens": 4000, "temperature": 0.3,
          "reasoning_effort": "low",
          "messages": [{"role": "system", "content": GEN_SYS},
                       {"role": "user",
                        "content": "公司违法把我辞退了，我月薪8000，2020年3月入职，能拿多少赔偿金？"}]})
if "_err" not in d:
    print(d["choices"][0]["message"].get("content") or "")
