# -*- coding: utf-8 -*-
"""MCP 服务回归：真起一个 stdio 子进程，按 JSON-RPC 逐条对话。

为什么不用纯函数 mock：stdio transport 最常见的故障全在进程边界上——
往 stdout 打了一行日志、通知也回了包、Windows 把 \n 变成 \r\n、
工具抛异常导致进程退出。这些都只有真跑一遍才看得见。

跑法：
    ./.venv/Scripts/python.exe scripts/test_mcp.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PY = sys.executable
PASS: list[str] = []
FAIL: list[str] = []


def ok(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" +
          (f"    {detail}" if detail and not cond else ""))


class Client:
    """一次会话里顺序发请求、收一行响应。"""

    def __init__(self):
        env = dict(os.environ)
        # 子进程必须真的没 key：MCP 服务不许依赖模型，这一条要在进程级验。
        env["DEEPSEEK_API_KEY"] = ""
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        self.p = subprocess.Popen(
            [PY, "-m", "app.mcp_server"], cwd=str(ROOT), env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding="utf-8",
            bufsize=1,
        )
        self.n = 0
        self.raw_lines: list[str] = []

    def send_raw(self, line: str) -> None:
        self.p.stdin.write(line + "\n")
        self.p.stdin.flush()

    def request(self, method: str, params: dict | None = None) -> dict:
        self.n += 1
        rid = self.n
        req = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            req["params"] = params
        self.send_raw(json.dumps(req, ensure_ascii=False))
        line = self.p.stdout.readline()
        self.raw_lines.append(line)
        return json.loads(line)

    def close(self) -> str:
        try:
            self.p.stdin.close()
            self.p.wait(timeout=10)
        except Exception:                               # noqa: BLE001
            self.p.kill()
            self.p.wait(timeout=5)
        return (self.p.stderr.read() or "")


def tool_text(resp: dict) -> str:
    return "".join(c.get("text", "") for c in
                   (resp.get("result") or {}).get("content") or [])


def tool_json(resp: dict) -> dict:
    try:
        return json.loads(tool_text(resp))
    except (ValueError, TypeError):
        return {}


def test_protocol(c: Client) -> None:
    print("\n[1] 协议：握手、通知、清单")
    r = c.request("initialize", {"protocolVersion": "2024-11-05",
                                 "capabilities": {},
                                 "clientInfo": {"name": "test", "version": "1"}})
    res = r.get("result") or {}
    ok("initialize 返回 2.0 响应", r.get("jsonrpc") == "2.0" and r.get("id") == 1)
    ok("回显客户端协议版本", res.get("protocolVersion") == "2024-11-05",
       str(res.get("protocolVersion")))
    ok("声明 tools 与 resources 能力",
       "tools" in res.get("capabilities", {}) and
       "resources" in res.get("capabilities", {}))
    ok("serverInfo 有名有版本",
       res.get("serverInfo", {}).get("name") == "law-agent" and
       bool(res.get("serverInfo", {}).get("version")))

    r2 = c.request("initialize", {"protocolVersion": "2099-01-01"})
    ok("不认识的版本回落到 2024-11-05",
       (r2.get("result") or {}).get("protocolVersion") == "2024-11-05")

    # 通知不该有响应：把它和 ping 连着发，读回来的第一条必须是 ping 的。
    c.send_raw(json.dumps({"jsonrpc": "2.0",
                           "method": "notifications/initialized"}))
    r3 = c.request("ping")
    ok("notifications/initialized 不回包",
       r3.get("id") == c.n and r3.get("result") == {}, str(r3)[:160])

    r4 = c.request("tools/list")
    tools = (r4.get("result") or {}).get("tools") or []
    names = [t["name"] for t in tools]
    ok("工具清单为 6 个", len(tools) == 6, str(names))
    ok("工具名不重复", len(set(names)) == 6)
    ok("每个工具有描述与 inputSchema",
       all(t.get("description") and t.get("inputSchema", {}).get("type") == "object"
           for t in tools))
    ok("合并后没有照搬内部 9 个工具",
       set(names) == {"search_statutes", "get_article", "calculate_compensation",
                      "calculate_deadline", "case_analysis",
                      "case_decision"}, str(names))
    ok("不暴露写文档 / 用户档案能力",
       not any(k in " ".join(names) for k in ("draft", "auth", "memory", "profile")))
    ok("描述里不承诺替用户填金额",
       any("留空" in t["description"] for t in tools
           if t["name"] == "case_analysis"))

    r5 = c.request("no/such/method")
    ok("未知方法报 -32601", (r5.get("error") or {}).get("code") == -32601)
    r6 = c.request("prompts/list")
    ok("未声明的 prompts 能力老实返回空表",
       (r6.get("result") or {}).get("prompts") == [])


def test_tools(c: Client) -> None:
    print("\n[2] 六个能力：真跑一遍")
    r = c.request("tools/call", {"name": "search_statutes",
                                 "arguments": {"query": "未订立书面劳动合同 二倍工资",
                                               "topk": 3}})
    ok("search_statutes 不是错误", (r.get("result") or {}).get("isError") is False,
       tool_text(r)[:160])
    d = tool_json(r)
    ok("检索结果带条文", bool(d.get("articles")), str(d)[:160])
    ok("检索结果带条号", "第八十二条" in tool_text(r) or "条" in tool_text(r))

    r = c.request("tools/call", {"name": "get_article",
                                 "arguments": {"law_name": "劳动合同法",
                                               "article_no": "第八十二条"}})
    d = tool_json(r)
    ok("get_article 取到原文", "二倍工资" in tool_text(r) or
       bool(d.get("articles")), tool_text(r)[:160])

    r = c.request("tools/call", {"name": "calculate_compensation",
                                 "arguments": {"monthly_wage": 12000,
                                               "start_date": "2022-01-01",
                                               "end_date": "2025-01-01",
                                               "region": "杭州"}})
    d = tool_json(r)
    ok("补偿计算返回 scenarios", bool((d.get("data") or {}).get("scenarios")),
       str(d)[:160])
    ok("补偿计算不出错", not d.get("error"), str(d.get("error")))

    r = c.request("tools/call", {"name": "calculate_deadline",
                                 "arguments": {"dispute": "dismissal",
                                               "onset": "2025-06-01"}})
    d = tool_json(r)
    ok("期限计算返回 ok", (d.get("data") or {}).get("ok") is True, str(d)[:160])
    ok("期限计算给了 candidates",
       bool((d.get("data") or {}).get("candidates")))

    r = c.request("tools/call", {"name": "case_analysis",
                                 "arguments": {"question": "公司没签劳动合同，能要什么",
                                               "slots": {"monthly_wage": 12000}}})
    d = tool_json(r)
    ok("办案包五块齐全",
       set(d) == {"timeline", "claims", "evidence_check",
                  "decision", "persona"}, str(list(d)))
    ok("办案包时间轴有 8 个节点", len(d.get("timeline", {}).get("nodes") or []) == 8)
    ok("办案包请求事项默认不勾",
       all(i.get("checked") is False for i in d.get("claims", {}).get("items") or []))
    ok("办案包金额留空", "＿＿＿＿" in tool_text(r))
    ok("办案包带免责", "不构成法律意见" in tool_text(r))

    r = c.request("tools/call", {"name": "case_decision",
                                 "arguments": {"question": "公司拖欠工资，时效快到了",
                                               "limit": 3}})
    dc = tool_json(r)
    ok("case_decision 不是错误",
       (r.get("result") or {}).get("isError") is False, tool_text(r)[:160])
    ok("决策卡带结局分布与样本数",
       (dc.get("similar") or {}).get("distribution", {}).get("total", 0) >= 1,
       str(dc)[:160])
    ok("决策卡带 6 行成本收益",
       len((dc.get("cost_benefit") or {}).get("rows") or []) == 6)
    ok("相似案件附官方处理结果原文",
       all(x.get("outcome_quote") for x in
           (dc.get("similar") or {}).get("items") or []))
    # v137：代理费/诉讼费是合法的法定费率，也带 %。判据收窄成
    # 「除了费率语境，一处都不许出现」——守的仍是「不给胜诉率」。
    from app.costs import bad_percentages as _bad_pct
    ok("决策卡不给胜诉率",
       not _bad_pct(tool_text(r)) and "不提供胜诉率" in tool_text(r),
       tool_text(r)[:200])


def test_errors(c: Client) -> None:
    print("\n[3] 错误路径：都不能把进程弄死")
    r = c.request("tools/call", {"name": "no_such_tool", "arguments": {}})
    ok("未知工具走 isError 而不是崩",
       (r.get("result") or {}).get("isError") is True, str(r)[:160])
    ok("未知工具列出可用工具", "可用工具" in tool_text(r))

    r = c.request("tools/call", {"name": "search_statutes", "arguments": {}})
    ok("缺 query 报工具级错误", (r.get("result") or {}).get("isError") is True)
    r = c.request("tools/call", {"name": "search_statutes",
                                 "arguments": {"query": "工资", "topk": 999}})
    ok("topk 越界被拦", (r.get("result") or {}).get("isError") is True)
    r = c.request("tools/call", {"name": "search_statutes",
                                 "arguments": {"query": "工资" * 600}})
    ok("超长 query 被拦", (r.get("result") or {}).get("isError") is True)
    r = c.request("tools/call", {"name": "calculate_compensation",
                                 "arguments": {"monthly_wage": "一万二",
                                               "start_date": "2022-01-01",
                                               "end_date": "2025-01-01"}})
    ok("月薪非整数被拦", (r.get("result") or {}).get("isError") is True)

    # 坏 JSON 是 transport 层唯一的解析错误，必须回 -32700 且继续服务。
    c.send_raw("{ this is not json")
    line = c.p.stdout.readline()
    c.raw_lines.append(line)
    try:
        r = json.loads(line)
    except ValueError:
        r = {}
    ok("坏 JSON 回 -32700", (r.get("error") or {}).get("code") == -32700, line[:120])
    r = c.request("ping")
    ok("坏 JSON 之后服务还活着", r.get("result") == {} and r.get("id") == c.n)


def test_resources(c: Client) -> None:
    print("\n[4] resources：只读，可核验")
    r = c.request("resources/list")
    rs = (r.get("result") or {}).get("resources") or []
    ok("两个资源", len(rs) == 2, str(rs)[:160])
    ok("资源都有 uri", all(x.get("uri") for x in rs))

    r = c.request("resources/read", {"uri": "law://extras/cites"})
    txt = "".join(x.get("text", "") for x in
                  (r.get("result") or {}).get("contents") or [])
    try:
        d = json.loads(txt)
    except ValueError:
        d = {}
    ok("办案包条号资源可读", d.get("count", 0) > 10 and bool(d.get("cites")),
       txt[:160])
    ok("条号资源里都是《法》第X条",
       all("《" in s and "条" in s for s in d.get("cites") or []))

    r = c.request("resources/read", {"uri": "law://corpus/stats"})
    txt = "".join(x.get("text", "") for x in
                  (r.get("result") or {}).get("contents") or [])
    ok("语料概况资源可读", '"ok": true' in txt, txt[:160])
    r = c.request("resources/read", {"uri": "law://nope"})
    ok("未知资源报 -32602", (r.get("error") or {}).get("code") == -32602)


def test_config_and_purity() -> None:
    print("\n[5] 配置与 stdout 纯净")
    p = ROOT / "mcp_config.json"
    ok("根目录有 mcp_config.json", p.exists())
    if p.exists():
        try:
            cfg = json.loads(p.read_text(encoding="utf-8"))
        except ValueError:
            cfg = {}
        srv = (cfg.get("mcpServers") or {}).get("law-agent") or {}
        ok("配置里命令是 python -m app.mcp_server",
           "-m" in (srv.get("args") or []) and
           "app.mcp_server" in (srv.get("args") or []), str(srv.get("args")))
        ok("配置里显式清空 key（不许依赖模型）",
           (srv.get("env") or {}).get("DEEPSEEK_API_KEY") == "")
        ok("配置里带 cwd 占位说明", "cwd" in srv)

    # 纯函数层：语料没加载成功时，case_analysis 仍必须可用。
    from app import mcp_server as M                              # noqa: PLC0415
    out = M.call_tool("case_analysis", {"question": "没签劳动合同"}, None)
    ok("语料不可用时 case_analysis 仍能出结果",
       len(out["timeline"]["nodes"]) == 8)
    dec = M.call_tool("case_decision", {"question": "拖欠工资"}, None)
    ok("语料不可用时 case_decision 仍能出结果",
       len((dec.get("cost_benefit") or {}).get("rows") or []) == 6)
    try:
        M.call_tool("search_statutes", {"query": "工资"}, None)
        ok("语料不可用时检索工具明确报不可用", False, "竟然没报错")
    except M.McpError as e:
        ok("语料不可用时检索工具明确报不可用", "法条库没加载成功" in str(e))
    ok("通知在 handle 层就返回 None",
       M.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}, None) is None)


def main() -> int:
    c = Client()
    stderr = ""
    try:
        test_protocol(c)
        test_tools(c)
        test_errors(c)
        test_resources(c)
        # 每一行 stdout 都必须是合法 JSON-RPC 响应；多一个字的日志都会让
        # 客户端在解析阶段就断线，而且报错通常完全指不到真因。
        ok("stdout 每一行都是合法响应",
           all(line.strip() and json.loads(line).get("jsonrpc") == "2.0"
               for line in c.raw_lines),
           str([ln[:60] for ln in c.raw_lines
                if not ln.strip() or "jsonrpc" not in ln]))
        ok("响应不重复也不丢 id",
           [json.loads(ln).get("id") for ln in c.raw_lines
            if json.loads(ln).get("id") is not None] ==
           sorted({json.loads(ln).get("id") for ln in c.raw_lines
                   if json.loads(ln).get("id") is not None}))
    finally:
        stderr = c.close()

    ok("stderr 没有 traceback（工具异常没漏到进程外）",
       "Traceback" not in stderr, stderr[-200:])
    test_config_and_purity()

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
