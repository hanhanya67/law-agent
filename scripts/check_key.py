# -*- coding: utf-8 -*-
"""上线前自检：确认 key 有效、模型名对、Function Calling 可用、余额够。

为什么单独写这个：
  演示当天最常见的翻车不是代码 bug，是 key 失效 / 模型名改了 / 余额没了 /
  厂商把 tools 参数换掉。这几种失败在正常流程里表现为「悄悄降级成回放模式」，
  页面照样出字，但答案是规则拼的——发现时已经晚了。

用法：
    .venv\\Scripts\\python.exe scripts\\check_key.py
返回码 0 全通过，1 有硬故障（鉴权 / 余额 / 模型名 / 正文为空），
2 表示「有一项没测出结果」——推理模型的思考会把 max_tokens 吃满，
这时候既不是通过也不是 key 坏了，重跑一次再判断。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import (DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL, DEEPSEEK_MODEL,
                        LLM_ENABLED)

OK = "[ OK ]"
BAD = "[FAIL]"
WARN = "[WARN]"


def main() -> int:
    print("=" * 58)
    print("大模型链路自检")
    print("=" * 58)

    print(f"base_url : {DEEPSEEK_BASE_URL}")
    print(f"model    : {DEEPSEEK_MODEL}")
    if not DEEPSEEK_API_KEY:
        print(f"{BAD} .env 里没有 DEEPSEEK_API_KEY，程序会跑回放模式")
        return 1
    print(f"api_key  : {DEEPSEEK_API_KEY[:6]}…{DEEPSEEK_API_KEY[-4:]}"
          f"（{len(DEEPSEEK_API_KEY)} 字符）")

    if not LLM_ENABLED:
        print(f"{BAD} LLM_ENABLED=False，配置没加载进来")
        return 1

    try:
        from app._api import MiniClient
        client = MiniClient(DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL)
    except Exception as e:                        # noqa: BLE001
        print(f"{BAD} 客户端初始化失败：{type(e).__name__}: {e}")
        return 1
    print(f"{OK} 客户端就绪（标准库 urllib，不依赖 openai SDK）")

    # ---- 1. 连通性与鉴权 ----
    print("\n[1/3] 基础对话")
    t0 = time.perf_counter()
    try:
        r = client.chat.completions.create(
            model=DEEPSEEK_MODEL,
            messages=[{"role": "user", "content": "只回复两个字：收到"}],
            max_tokens=200, temperature=0)
    except Exception as e:                        # noqa: BLE001
        print(f"{BAD} {type(e).__name__}: {e}")
        _hint(e)
        return 1
    dt = (time.perf_counter() - t0) * 1000
    txt = (r.choices[0].message.content or "").strip()
    if not txt:
        # 推理模型的思考与正文共享 max_tokens。预算给小了就是「思考吃满、
        # 正文 0 字」，而 HTTP 是 200——不单独判这一条，这一项会印成
        # `[ OK ] 1200 ms，返回：''`，看着通过，其实什么都没验到。
        fr = getattr(r.choices[0], "finish_reason", "")
        print(f"{BAD} {dt:.0f} ms，正文为空（finish_reason={fr!r}）。"
              f"多半是思考吃满了 max_tokens，不是鉴权失败")
        return 1
    print(f"{OK} {dt:.0f} ms，返回：{txt!r}")
    _usage(r, "基础对话")

    # ---- 2. Function Calling ----
    # 这一步最关键：整个 Agent 的决策层依赖原生 tool_calls。
    # 如果厂商关了或改了参数，决策就只剩「模型输出一段文字，我们正则去猜」，
    # 于是「Agent」退化成一个 if-else 外壳——评审一眼能看穿。
    print("\n[2/3] Function Calling")
    tools = [{
        "type": "function",
        "function": {
            "name": "search_statute",
            "description": "按关键词检索法条",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string", "description": "检索词"}},
                "required": ["query"],
            },
        },
    }]
    try:
        r2 = client.chat.completions.create(
            model=DEEPSEEK_MODEL,
            messages=[{"role": "user",
                       "content": "帮我查一下劳动合同法关于试用期期限的规定"}],
            tools=tools, tool_choice="auto",
            # 200 太小：思考与正文共享预算，实测这一问的思考就可能把 200 吃光，
            # 于是既没有 tool_calls 也没有正文，而下面会把它读成「tools 参数
            # 不被支持」——一句把人引向错误结论的假红。
            max_tokens=800, temperature=0)
    except Exception as e:                        # noqa: BLE001
        print(f"{BAD} {type(e).__name__}: {e}")
        print("       tools 参数可能不被该模型支持，Agent 决策层需要改方案")
        return 1
    msg = r2.choices[0].message
    calls = getattr(msg, "tool_calls", None) or []
    body = (msg.content or "").strip()
    if calls:
        c = calls[0]
        print(f"{OK} 模型主动调用了 {c.function.name}，参数 {c.function.arguments}")
    elif not body:
        # 两样都没有 = 这次调用什么都没产出，不能据此说 tools 不支持。
        fr = getattr(r2.choices[0], "finish_reason", "")
        print(f"{WARN} 既没有 tool_calls 也没有正文（finish_reason={fr!r}），"
              f"这一项没测出结果，不代表 tools 参数有问题")
        print("       先确认余额与 max_tokens；重跑一次仍是这样就再看工具定义")
    else:
        print(f"{WARN} 模型没调工具，直接回话：{body[:60]}")
        print("       不一定是故障（简单问题模型可能直接答），但演示问题应能触发工具")
    _usage(r2, "决策调用")

    # ---- 3. 流式 ----
    print("\n[3/3] 流式输出")
    t0 = time.perf_counter()
    first = None
    buf: list[str] = []
    try:
        st = client.chat.completions.create(
            model=DEEPSEEK_MODEL,
            messages=[{"role": "user", "content": "用一句话说明什么是经济补偿"}],
            # 120 原来是拍的：推理模型先吐 reasoning_content、再吐 content，
            # 两者共享这一份预算，预算不够时整段思考就把正文吃没了——一个
            # content 分片都收不到，下面 `first` 一直是 None。
            max_tokens=400, temperature=0.3, stream=True)
        for part in st:
            d = part.choices[0].delta.content if part.choices else None
            if d:
                if first is None:
                    first = (time.perf_counter() - t0) * 1000
                buf.append(d)
    except Exception as e:                        # noqa: BLE001
        print(f"{BAD} {type(e).__name__}: {e}")
        return 1
    total = (time.perf_counter() - t0) * 1000
    if first is None:
        # 这里原来直接 `f"{first:.0f}"`，一个正文分片都没收到时抛
        # TypeError: unsupported format string passed to NoneType.__format__，
        # 带着 traceback 以退出码 1 结束——把「这一项没测出东西」报成
        # 「key 坏了」。演示前照着这个红去换 key，能折腾半天。
        print(f"{WARN} {total:.0f} ms 内一个正文分片都没收到（思考可能吃满了"
              f" max_tokens），这一项没测出结果，不代表 key 失效")
        print("       重跑一次；连续两次都这样就别用这份 key 演示了")
        print("\n" + "=" * 58)
        print("结论：两项通过，流式这一项未测出。先重跑，别急着换 key。")
        print("=" * 58)
        return 2
    print(f"{OK} 首字 {first:.0f} ms，总 {total:.0f} ms，{len(''.join(buf))} 字")
    if first > 3000:
        print(f"{WARN} 首字超过 3 秒，演示时前端会显得卡")

    print("\n" + "=" * 58)
    print("结论：链路可用。可以跑 eval/run_eval.py --online")
    print("=" * 58)
    return 0


def _usage(r, label: str) -> None:
    u = getattr(r, "usage", None)
    if not u:
        return
    # 自建客户端返回的是原始 dict，SDK 返回的是对象，两种都兼容
    get = u.get if isinstance(u, dict) else lambda k, d=0: getattr(u, k, d)
    print(f"        {label}: 输入 {get('prompt_tokens')} / 输出 "
          f"{get('completion_tokens')} / 合计 {get('total_tokens')} token")


def _hint(e: Exception) -> None:
    """把常见报错翻译成下一步动作，省得对着 traceback 猜。"""
    s = f"{type(e).__name__}: {e}".lower()
    if "401" in s or "authentication" in s or "invalid api key" in s:
        print("       → key 无效或已被删除，去 platform.deepseek.com 重新生成")
    elif "402" in s or "insufficient" in s or "balance" in s:
        print("       → 余额不足，去平台充值")
    elif "404" in s or "model" in s and "not found" in s:
        print(f"       → 模型名 {DEEPSEEK_MODEL} 不存在，改 .env 的 DEEPSEEK_MODEL")
    elif "timeout" in s or "connect" in s:
        print("       → 网络不通，检查代理或 DNS")
    elif "429" in s:
        print("       → 触发限流，等几十秒再试")


if __name__ == "__main__":
    raise SystemExit(main())
