# -*- coding: utf-8 -*-
"""
命令行入口。基础版就用它验证 Agent，不碰前端。

为什么先做 CLI：Agent 的问题大多出在"决策链"上，而不是"页面"上。
CLI 把每一步工具调用、耗时、错误都摊开打印，比在浏览器里翻日志快十倍。
Web 界面是后面的事。

用法：
    python -m app.cli
    python -m app.cli "公司没签合同还拖欠工资，我能要什么"
"""
from __future__ import annotations

import os
import sys
import uuid

os.system("")          # 启用 Windows 控制台的 ANSI 转义

from .agent import AgentRunner
from .config import (CORPUS_PATH, DB_PATH, LLM_ENABLED, MAX_STEPS,
                     MODE_NOTE, RETRIEVAL_BACKEND)
from .llm import LLM
from .memory import MemoryStore
from .retrieval import LawIndex

DIM, BOLD, RESET = "\033[2m", "\033[1m", "\033[0m"
CYAN, GREEN, YELLOW, RED, MAGENTA = ("\033[36m", "\033[32m",
                                     "\033[33m", "\033[31m", "\033[35m")

BANNER = r"""
  ┌──────────────────────────────────────────────────────────┐
  │   多领域法规问答 · 命令行 MVP                            │
  │   大模型 + 规划 + 记忆 + 工具（默认劳动法内核）          │
  └──────────────────────────────────────────────────────────┘
"""


def _pr(color: str, text: str) -> None:
    print(f"{color}{text}{RESET}")


def make_runner() -> tuple[AgentRunner, MemoryStore]:
    index = LawIndex(CORPUS_PATH)
    store = MemoryStore(DB_PATH)
    runner = AgentRunner(index, LLM(), store)
    return runner, store


class Painter:
    """把 SSE 事件画成终端里能读的轨迹。同时服务于 Web 前端的事件顺序。"""

    def __init__(self) -> None:
        self._streaming = False

    def _close_stream(self) -> None:
        if self._streaming:
            print()
            self._streaming = False

    def __call__(self, event: str, data: dict) -> None:
        if event == "token":
            if not self._streaming:
                print(f"\n  {BOLD}[答案]{RESET} ", end="", flush=True)
                self._streaming = True
            print(data["text"], end="", flush=True)
            return

        self._close_stream()

        if event == "intent":
            _pr(MAGENTA, f"  [意图] {data['intent']}")
        elif event == "plan":
            _pr(MAGENTA, "  [计划] " + " / ".join(data["queries"]))
        elif event == "context":
            _pr(CYAN, f"  [记忆] 这是补充信息，沿用上一问：{data['carried_from']}")
        elif event == "memory":
            if data["changed"]:
                pairs = "，".join(f"{k}={v}" for k, v in data["slots"].items()
                                 if k in data["changed"])
                _pr(CYAN, f"  [记忆] 记下 {pairs}")
        elif event == "tool_start":
            args = ", ".join(f"{k}={v!r}" for k, v in data["args"].items())
            print(f"{YELLOW}  [{data['step']}] {data['tool']}{RESET}"
                  f"({DIM}{args}{RESET})")
        elif event == "tool_end":
            flag = f" {RED}错误：{data['error']}{RESET}" if data.get("error") else ""
            print(f"      {GREEN}↳{RESET} {data['summary']} "
                  f"{DIM}({data['ms']}ms){RESET}{flag}")
        elif event == "verdict":
            tone = GREEN if data["kind"] == "legal" else RED
            if data["kind"] == "unknown":
                tone = YELLOW
            mark = {"illegal": "✗", "claimable": "!", "legal": "✓",
                    "conditional": "?", "unknown": "·"}.get(data["kind"], "·")
            _pr(tone, f"  [定性] {mark} {data['label']}")
            if data.get("legal"):
                _pr(DIM, f"         {data['legal']}")
        elif event == "verify":
            if data["passed"]:
                _pr(GREEN, f"  [自检] 通过 · 证据 {len(data['citations'])} 条")
            else:
                _pr(RED, f"  [自检] 发现 {len(data['issues'])} 处无依据引用")
        elif event == "note":
            print(f"  {DIM}{data['text'].strip()}{RESET}")
        elif event == "done":
            print(f"  {DIM}— {data['steps']} 步 · {data['elapsed_ms']}ms · "
                  f"{data['status']} —{RESET}")
        elif event == "error":
            _pr(RED, f"  [错误] {data.get('message')}")


def ask(runner: AgentRunner, question: str, session_id: str) -> None:
    print()
    _pr(BOLD, f"你 > {question}")
    runner.run(question, session_id=session_id, emit=Painter())
    print()


def main() -> None:
    print(BANNER)

    try:
        index = LawIndex(CORPUS_PATH)
    except FileNotFoundError as e:
        _pr(RED, str(e))
        _pr(YELLOW, "先运行：python scripts/make_seed.py")
        sys.exit(1)

    store = MemoryStore(DB_PATH)
    llm = LLM()
    runner = AgentRunner(index, llm, store)

    st = index.stats()
    _pr(DIM, f"  语料：{st['articles']} 条条文 · {len(st['laws'])} 部法律 · "
             f"引用边 {st['citation_edges']} 条")
    _pr(DIM, f"  检索：{RETRIEVAL_BACKEND}（纯 Python BM25，零依赖）")
    mode_color = GREEN if LLM_ENABLED else YELLOW
    # 这行是**配置**结论，不是「现在通不通」。余额扣光、key 被停都发生在启动
    # 之后，所以在线也要写清楚「等第一次调用才知道」——否则用户会以为已经验过了。
    _pr(mode_color, f"  模型：{MODE_NOTE}"
                    + ("（等第一次提问验证；失败会自动转回放并提示）"
                       if LLM_ENABLED else ""))
    if not LLM_ENABLED:
        _pr(DIM, "        在 .env 里填 DEEPSEEK_API_KEY 即可切到真实模型，"
                 "当前回放模式同样会走完整决策循环")
    _pr(DIM, "  命令：/new 新会话  /slots 查看记忆  /trace 查看轨迹  /quit 退出")
    print()

    session_id = uuid.uuid4().hex[:8]

    # 支持一行式提问，方便脚本化测试
    if len(sys.argv) > 1:
        ask(runner, " ".join(sys.argv[1:]), session_id)
        return

    while True:
        try:
            line = input(f"{CYAN}>{RESET} ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line:
            continue
        if line in ("/quit", "/exit", "/q"):
            break
        if line == "/new":
            session_id = uuid.uuid4().hex[:8]
            _pr(GREEN, f"  新会话 {session_id}")
            continue
        if line == "/slots":
            slots = store.load_slots(session_id)
            known = slots.known()
            _pr(DIM, f"  会话 {session_id} 记忆：" +
                     (str(known) if known else "（空）"))
            continue
        if line == "/trace":
            rows = store.traces(session_id, limit=20)
            if not rows:
                _pr(DIM, "  暂无轨迹")
            for r in rows:
                print(f"  {DIM}#{r['id']} turn{r['turn']} "
                      f"[{r['step_index']}] {r['tool']} "
                      f"({r['elapsed_ms']}ms) {r['summary']}{RESET}")
            continue
        if line == "/help":
            _pr(DIM, "  /new /slots /trace /quit")
            continue
        ask(runner, line, session_id)

    store.close()
    _pr(DIM, "  再见。")


if __name__ == "__main__":
    main()
