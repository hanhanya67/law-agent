# -*- coding: utf-8 -*-
"""降级 → 冷却 → 恢复 → 真的又能在线：这条链只有账户有钱时才验得到。

为什么要单独一个脚本：它需要一个**真的会失败的调用**来触发降级，而余额充足时
永远不会有失败。余额长期为 0 的那些日子里，这条链只能靠替体验——
`test_feedback.py` 钉住了 `maybe_recover()` 的返回值，但「恢复之后真的又拿到
模型答案」这一步从来没人验过，而那恰恰是用户能感知的那一步
（「我充了值，为什么还是回放」）。

怎么造一个真实的失败：换一个坏 key 去打接口，拿一个真的 401。
不 mock、不注入异常——mock 出来的是「代码以为自己降级了」，
真 401 才是「接口真的拒绝了」。

跑法（会花钱，但只有两三次极小调用）：

    ./.venv/Scripts/python.exe scripts/check_recover.py

没配 key 时它直接说清楚并退出 0（属于「这个环境验不了」，不是失败）。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:            # noqa: BLE001
    pass

from app.config import (DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL,  # noqa: E402
                        LLM_ENABLED)
from app.llm import LLM                                       # noqa: E402

PASS, FAIL = [], []


def ok(name: str, cond: bool, detail: object = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}"
          + (f"  → {detail}" if detail != "" else ""))


def main() -> int:
    print("=" * 62)
    print("  降级 → 冷却 → 恢复（需要真实可用的 key）")
    print("=" * 62)

    if not LLM_ENABLED:
        print("  没配置 key，验不了。这条链必须有真实的成功与真实的失败。")
        print("  （不是失败：是这个环境不具备条件）")
        return 0

    llm = LLM()
    if not llm.online:
        print("  配了 key 但客户端没起来，验不了。")
        return 0

    # ---- 1. 先确认现在是真在线：拿一次真实调用的结果
    r = llm.classify_intent("公司拖欠工资怎么办", slots_brief="")
    ok("起始是在线", llm.online, f"意图={r}")

    # ---- 2. 换成坏 key，制造一次真实失败
    from app._api import MiniClient  # noqa: PLC0415
    good = llm._client
    llm._client = MiniClient("sk-this-key-is-not-real", DEEPSEEK_BASE_URL)
    r2 = llm.classify_intent("公司拖欠工资怎么办", slots_brief="")
    ok("接口拒绝后降级为离线", not llm.online, f"降级理由={llm.fallback_reason[:40]}")
    ok("降级后仍然给了答案（走规则兜底）", bool(r2), f"意图={r2}")

    # ---- 3. 冷却期内不重试
    import app.llm as llm_mod  # noqa: PLC0415
    llm_mod.LLM_RECOVER_AFTER = 2          # 默认 600 秒，等不起
    ok("冷却期内不重试", llm.maybe_recover() is False and not llm.online)

    # ---- 4. 降级之后应当真的不碰接口了
    #      用「数几次网络调用」来判，而不是看返回值——规则兜底和模型答案在
    #      case_consult 这种意图上是同一个结果，比返回值等于没比。
    calls: list[int] = []
    real_create = good.chat.completions.create

    def counted(**kw):
        calls.append(1)
        return real_create(**kw)

    llm._client = MiniClient(DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL)
    llm._client.chat.completions.create = counted
    calls.clear()
    llm.classify_intent("公司拖欠工资怎么办", slots_brief="")
    ok("降级后不再打扰接口（真的走离线）", len(calls) == 0, f"{len(calls)} 次调用")

    # ---- 5. 冷却过后恢复：换回好 key，它应当真的又去打接口
    llm._client.chat.completions.create = real_create
    time.sleep(2.4)
    ok("冷却过后重新试探", llm.maybe_recover() is True and llm.online)

    llm._client.chat.completions.create = counted
    calls.clear()
    llm.classify_intent("公司拖欠工资怎么办", slots_brief="")
    ok("恢复后这次真打到接口了", len(calls) >= 1, f"{len(calls)} 次调用")
    ok("恢复后仍是在线", llm.online)

    print("\n" + "=" * 62)
    print(f"  {len(PASS)} / {len(PASS) + len(FAIL)} 通过")
    for f in FAIL:
        print("  失败：" + f)
    print("=" * 62)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())
