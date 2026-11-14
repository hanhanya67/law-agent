# -*- coding: utf-8 -*-
"""MCP 服务：把本项目的只读能力开放给外部 Agent 客户端。

跑法：
    python -m app.mcp_server                 # stdio，由 MCP 客户端拉起
    python -m app.mcp_server --self-check   # 本机自检，不进协议循环

设计上刻意守住四条边界（每一条都是「出了事就很难收回」的那种）：

  1. **只读**。不写数据库、不读用户会话、不读个人档案。MCP 客户端能拿到的
     只有公开法条 + 由参数现算出来的办案包。所以这里不暴露 draft_document
     （它会带姓名、单位名称），也不暴露任何 memory / auth 接口。
  2. **不照搬内部 9 个工具**。那是给本项目的小模型决策循环用的，描述里
     写着「什么时候必须调用本工具」这类内部控制语，外部客户端看了只会乱调。
     这里按**能力**重新合并成 5 个，边界写给人（和更强的模型）看。
  3. **stdout 只说协议**。任何日志、警告、traceback 走 stderr。stdio transport
     里往 stdout 多打一行，客户端就会把它当一条坏消息，而且报错通常指向
     「JSON 解析失败」，看不出真因。
  4. **不联网、不调模型**。MCP 客户端自带模型；本服务只提供法条与确定性
     计算。所以没配 key、断网、模型欠费，这里照样能答。

协议：JSON-RPC 2.0，newline-delimited JSON（MCP stdio transport）。
实现的是 2024-11-05 协商版本，客户端带别的版本时回显它认识的那一个。
"""
from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import extras                                          # noqa: E402
from app.config import CORPUS_PATH                               # noqa: E402
from app.retrieval import LawIndex                               # noqa: E402
from app.tools import ToolContext, dispatch                      # noqa: E402

PROTOCOL = "2024-11-05"
SUPPORTED = ("2024-11-05", "2025-03-26", "2025-06-18")
SERVER_NAME = "law-agent"
SERVER_VERSION = "1.0.0"

MAX_TEXT = 500          # 单个文本参数上限，防客户端塞一整本书进来
MAX_TOPK = 20


def log(msg: str) -> None:
    """只往 stderr 写。stdout 是协议通道，一个字节都不能占。"""
    print(f"[mcp] {msg}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------- 工具清单

TOOLS: tuple[dict[str, Any], ...] = (
    {
        "name": "search_statutes",
        "description": (
            "在劳动法/劳动合同法/民法典等现行法律条文中做 BM25 检索。"
            "需要找「哪条法管这件事」时用。返回条号、状态（是否现行有效）与原文摘要；"
            "只返回本库收录的条文，不会补写库里没有的内容。"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "检索语句，法律术语更好"},
                "topk": {"type": "integer", "description": "返回条数，1-20，默认 5"},
                "only_valid": {"type": "boolean",
                               "description": "是否只返回现行有效条文，默认 true"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_article",
        "description": (
            "按法名 + 条号取一条条文的完整原文。search_statutes 拿到条号后，"
            "引用前应当用它核一次原文。条号支持「第四十七条」和「47」两种写法。"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "law_name": {"type": "string", "description": "法律名称，简称即可"},
                "article_no": {"type": "string", "description": "条号"},
            },
            "required": ["law_name", "article_no"],
        },
    },
    {
        "name": "calculate_compensation",
        "description": (
            "按工作年限、月工资、所在城市计算经济补偿 / 违法解除赔偿金的月数与金额。"
            "含三倍封顶与十二年上限判断；地区参数查不到时只给公式、不编数字。"
            "region 一定要传，否则算不出可采信的金额。"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "monthly_wage": {"type": "integer", "description": "离职前十二个月平均月工资（元）"},
                "start_date": {"type": "string", "description": "入职日期 YYYY-MM[-DD]"},
                "end_date": {"type": "string", "description": "离职日期 YYYY-MM[-DD]"},
                "leave_reason": {"type": "string",
                                 "description": "公司辞退 / 违法辞退 / 协商解除 / 被迫离职 / 主动辞职"},
                "region": {"type": "string", "description": "城市名，如 北京 / 上海 / 杭州"},
            },
            "required": ["monthly_wage", "start_date", "end_date"],
        },
    },
    {
        "name": "calculate_deadline",
        "description": (
            "按民法典期间规则推算仲裁时效、起诉期限等法定截止日。"
            "在职讨薪不受一年时效限制，此时**不会**给出截止日。"
            "relation_ended 不确定就传 null，不要替用户猜。"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "dispute": {"type": "string",
                            "enum": ["wage", "dismissal", "no_contract",
                                     "arbitration_award", "arbitration_other",
                                     "employer_revoke", "injury"],
                            "description": "争议类型"},
                "onset": {"type": "string", "description": "起算日 YYYY-MM-DD"},
                "relation_ended": {"type": ["boolean", "null"],
                                   "description": "劳动关系是否已结束，仅 wage 有意义"},
                "end_date": {"type": "string", "description": "劳动关系终止日"},
                "as_of": {"type": "string", "description": "基准日，留空按今天"},
            },
            "required": ["dispute", "onset"],
        },
    },
    {
        "name": "case_analysis",
        "description": (
            "按案情给出一整套「办案包」：法定程序时间轴、可勾选的仲裁请求事项候选、"
            "举证体检清单。纯规则生成，不调用模型。请求事项的金额一律留空"
            "（正式申请书必须本人确认后填写），候选默认一条都不勾。"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "question": {"type": "string", "description": "用户的问题或案情描述"},
                "slots": {"type": "object",
                          "description": "已知案情要素：region/employment_type/"
                                         "has_contract/start_date/end_date/"
                                         "monthly_wage/leave_reason/social_insurance"},
                "verdict": {"type": "object", "description": "可选的定性结果 label/legal/reason"},
                "deadline_data": {"type": "object",
                                  "description": "可选的 calculate_deadline 返回值，用来对齐具体日期"},
                "calc_data": {"type": "object",
                              "description": "可选的 calculate_compensation 返回值"},
            },
            "required": ["question"],
        },
    },
    {
        "name": "case_decision",
        "description": (
            "判例参考与申诉决策卡：按争议类型检索本地收录的人社部/最高法"
            "劳动人事争议典型案例，给出相似案件的结局分布（支持/部分支持/驳回，"
            "附官方处理结果原文与来源），并把仲裁耗时、费用、可得金额并排列出。"
            "**不提供胜诉率、不预测结果、不建议是否申诉**，判断留给当事人。"
            "纯规则、离线、不调模型，不需要法条库加载成功。"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "question": {"type": "string", "description": "用户的问题或案情描述"},
                "stance": {"type": "string",
                           "description": "显式立场：employee/employer/agent/explore；"
                                          "不传按职工视角", "enum": ["employee", "employer", "agent", "explore"]},
                "slots": {"type": "object", "description": "已知案情要素，同 case_analysis"},
                "calc_data": {"type": "object",
                              "description": "可选的 calculate_compensation 返回值"},
                "limit": {"type": "integer", "description": "返回相似案件数，1-8，默认 4"},
            },
            "required": ["question"],
        },
    },
)

RESOURCES: tuple[dict[str, Any], ...] = (
    {"uri": "law://corpus/stats", "name": "法条库概况",
     "mimeType": "application/json",
     "description": "本库收录了多少条条文、涉及哪几部法律"},
    {"uri": "law://extras/cites", "name": "办案包引用的条号",
     "mimeType": "application/json",
     "description": "时间轴 / 请求事项 / 举证体检里出现的全部条号，便于外部逐条核验"},
)


# ---------------------------------------------------------------- 执行

class McpError(Exception):
    """工具执行失败。转成 isError，不让进程退出。"""

    def __init__(self, message: str, code: int = -32602):
        super().__init__(message)
        self.code = code


def _text(value: Any) -> str:
    """统一 JSON 序列化，中文不转义，外部客户端才读得懂条号。"""
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


def _limit_str(raw: Any, field: str) -> str:
    if not isinstance(raw, str):
        raise McpError(f"{field} 必须是字符串")
    s = raw.strip()
    if not s:
        raise McpError(f"{field} 不能为空")
    if len(s) > MAX_TEXT:
        raise McpError(f"{field} 太长（上限 {MAX_TEXT} 字），请缩短")
    return s


def _tool_result(res: Any) -> dict[str, Any]:
    """把内部 ToolResult 摊成外部能读的 dict（不暴露内部对象）。"""
    out: dict[str, Any] = {"summary": res.summary, "data": res.data}
    if res.error:
        out["error"] = res.error
    evs = []
    for e in res.evidence or []:
        evs.append({
            "article_id": e.article_id,
            "cite": e.cite(),
            "article_no": e.article_no_cn,
            "status": e.status,
            "content": e.content,
        })
    if evs:
        out["articles"] = evs
    return out


def call_tool(name: str, args: dict, index: LawIndex | None) -> dict[str, Any]:
    """六个只读能力的实现。统一从这里出，协议层不碰业务逻辑。"""
    args = args or {}
    if not isinstance(args, dict):
        raise McpError("arguments 必须是对象")

    if name == "case_decision":
        # 和 case_analysis 一样放在 index 判空之前：判例库是独立的
        # 本地 JSONL，不依赖语料索引。解压到新目录也能立刻用。
        from . import cases as _cases
        q = _limit_str(args.get("question", ""), "question")
        slots = args.get("slots") or {}
        if not isinstance(slots, dict):
            raise McpError("slots 必须是对象")
        lim = args.get("limit", 4)
        if not isinstance(lim, int) or isinstance(lim, bool) or not 1 <= lim <= 8:
            raise McpError("limit 必须是 1..8 的整数")
        return _cases.build_decision(
            q, stance=str(args.get("stance") or "employee"), slots=slots,
            calc_data=(args.get("calc_data")
                       if isinstance(args.get("calc_data"), dict) else None),
            limit=lim)

    if name == "case_analysis":
        # 办案包是纯函数，不依赖语料库加载成功。这一条尤其重要：
        # 用户把包解压到一个还没建索引的目录时，MCP 不该整个哑掉。
        q = _limit_str(args.get("question", ""), "question")
        slots = args.get("slots") or {}
        if not isinstance(slots, dict):
            raise McpError("slots 必须是对象")
        return extras.from_case(
            q, slots=slots,
            verdict=args.get("verdict") if isinstance(args.get("verdict"), dict) else None,
            deadline_data=(args.get("deadline_data")
                           if isinstance(args.get("deadline_data"), dict) else None),
            calc_data=(args.get("calc_data")
                       if isinstance(args.get("calc_data"), dict) else None),
        )

    if index is None:
        raise McpError("法条库没加载成功（data/statutes.jsonl 缺失或格式不对），"
                       "该工具暂时不可用；case_analysis / case_decision 仍可使用", -32603)

    ctx = ToolContext(index=index, state=None, profile={})

    if name == "search_statutes":
        q = _limit_str(args.get("query", ""), "query")
        topk = args.get("topk", 5)
        if not isinstance(topk, int) or isinstance(topk, bool) or not 1 <= topk <= MAX_TOPK:
            raise McpError(f"topk 必须是 1..{MAX_TOPK} 的整数")
        res = dispatch("search_statute",
                       {"query": q, "topk": topk,
                        "only_valid": bool(args.get("only_valid", True))}, ctx)
        return _tool_result(res)

    if name == "get_article":
        res = dispatch("get_article",
                       {"law_name": _limit_str(args.get("law_name", ""), "law_name"),
                        "article_no": _limit_str(args.get("article_no", ""), "article_no")},
                       ctx)
        return _tool_result(res)

    if name == "calculate_compensation":
        wage = args.get("monthly_wage")
        if not isinstance(wage, int) or isinstance(wage, bool) or wage <= 0:
            raise McpError("monthly_wage 必须是正整数（元）")
        payload = {
            "monthly_wage": wage,
            "start_date": _limit_str(args.get("start_date", ""), "start_date"),
            "end_date": _limit_str(args.get("end_date", ""), "end_date"),
            "leave_reason": str(args.get("leave_reason") or "公司辞退"),
            "region": str(args.get("region") or ""),
        }
        return _tool_result(dispatch("calculate_compensation", payload, ctx))

    if name == "calculate_deadline":
        payload = {
            "dispute": _limit_str(args.get("dispute", ""), "dispute"),
            "onset": _limit_str(args.get("onset", ""), "onset"),
            "relation_ended": args.get("relation_ended"),
            "end_date": str(args.get("end_date") or ""),
            "as_of": str(args.get("as_of") or ""),
        }
        return _tool_result(dispatch("calculate_deadline", payload, ctx))

    raise McpError(f"未知工具：{name}。可用工具：" +
                   "、".join(t["name"] for t in TOOLS), -32602)


def read_resource(uri: str, index: LawIndex | None) -> dict[str, Any]:
    if uri == "law://corpus/stats":
        return {"ok": index is not None,
                "stats": index.stats() if index is not None else None,
                "corpus": str(CORPUS_PATH)}
    if uri == "law://extras/cites":
        return {"count": len(extras.CITES), "cites": extras.cited_articles()}
    raise McpError(f"未知资源：{uri}", -32602)


# ---------------------------------------------------------------- 协议

def ok_result(rid: Any, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def err_result(rid: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": rid,
            "error": {"code": code, "message": message}}


def handle(req: Any, index: LawIndex | None) -> dict | None:
    """一条请求 → 一条响应；通知返回 None（协议规定不应答）。"""
    if not isinstance(req, dict):
        return err_result(None, -32600, "Invalid Request：请求必须是对象")
    rid = req.get("id")
    method = req.get("method")
    is_notification = "id" not in req
    if req.get("jsonrpc") != "2.0" or not isinstance(method, str):
        return (None if is_notification else
                err_result(rid, -32600, "Invalid Request：缺 jsonrpc 或 method"))

    if method == "initialize":
        p = req.get("params") or {}
        want = p.get("protocolVersion") if isinstance(p, dict) else None
        return ok_result(rid, {
            "protocolVersion": want if want in SUPPORTED else PROTOCOL,
            "capabilities": {"tools": {"listChanged": False},
                             "resources": {"subscribe": False, "listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        })

    if method.startswith("notifications/") or is_notification:
        # 初始化完成通知、取消通知等：不回包。
        return None

    if method == "ping":
        return ok_result(rid, {})

    if method == "tools/list":
        return ok_result(rid, {"tools": list(TOOLS)})

    if method == "tools/call":
        p = req.get("params") or {}
        if not isinstance(p, dict) or not p.get("name"):
            return err_result(rid, -32602, "tools/call 需要 params.name")
        try:
            data = call_tool(p["name"], p.get("arguments") or {}, index)
            return ok_result(rid, {"content": [{"type": "text", "text": _text(data)}],
                                   "isError": False})
        except McpError as e:
            # 工具级错误按 MCP 约定走 isError，而不是 JSON-RPC error：
            # 客户端的模型需要看到「为什么这次调用没成」才能自己改参数。
            return ok_result(rid, {"content": [{"type": "text", "text": e.args[0]}],
                                   "isError": True})
        except Exception as e:                          # noqa: BLE001
            log(f"tool {p.get('name')} 抛异常：{e}")
            log(traceback.format_exc(limit=3))
            return ok_result(rid, {
                "content": [{"type": "text", "text": f"{type(e).__name__}: {e}"}],
                "isError": True})

    if method == "resources/list":
        return ok_result(rid, {"resources": list(RESOURCES)})

    if method == "resources/read":
        p = req.get("params") or {}
        uri = p.get("uri") if isinstance(p, dict) else None
        try:
            data = read_resource(str(uri or ""), index)
            return ok_result(rid, {"contents": [
                {"uri": uri, "mimeType": "application/json", "text": _text(data)}]})
        except McpError as e:
            return err_result(rid, e.code, e.args[0])

    if method == "prompts/list":
        # 不宣称自己有 prompts 能力，返回空表比假装支持再失败好。
        return ok_result(rid, {"prompts": []})

    return err_result(rid, -32601, f"Method not found: {method}")


def load_index() -> LawIndex | None:
    try:
        idx = LawIndex(CORPUS_PATH)
        if not idx.stats().get("articles"):
            log("语料为空，法条类工具不可用")
            return None
        return idx
    except Exception as e:                              # noqa: BLE001
        log(f"法条库加载失败：{e}（case_analysis 仍可用）")
        return None


def serve(stdin=None, stdout=None) -> int:
    """协议循环。stdin/stdout 可注入，方便自检。"""
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    try:
        stdout.reconfigure(encoding="utf-8", newline="\n")
    except (AttributeError, ValueError):
        pass
    index = load_index()
    log(f"就绪（法条库 {'已加载' if index else '不可用'}，工具 {len(TOOLS)} 个）")
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except ValueError:
            stdout.write(json.dumps(err_result(None, -32700, "Parse error")) + "\n")
            stdout.flush()
            continue
        try:
            resp = handle(req, index)
        except Exception as e:                          # noqa: BLE001
            log(f"handle 抛异常：{e}")
            resp = err_result(req.get("id") if isinstance(req, dict) else None,
                              -32603, f"Internal error: {type(e).__name__}: {e}")
        if resp is not None:
            stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            stdout.flush()
    return 0


def self_check() -> int:
    """不进协议循环的本机自检：语料、工具清单、六个能力各跑一次。"""
    idx = load_index()
    print(f"法条库：{'已加载' if idx else '不可用'}")
    print(f"工具 {len(TOOLS)} 个：" + "、".join(t["name"] for t in TOOLS))
    for t in TOOLS:
        print(f"  - {t['name']}: {t['description'][:38]}…")
    probes = [
        ("search_statutes", {"query": "未订立书面劳动合同 二倍工资", "topk": 2}),
        ("get_article", {"law_name": "劳动合同法", "article_no": "第八十二条"}),
        ("calculate_compensation", {"monthly_wage": 12000,
                                    "start_date": "2022-01-01",
                                    "end_date": "2025-01-01", "region": "杭州"}),
        ("calculate_deadline", {"dispute": "dismissal", "onset": "2025-06-01"}),
        ("case_analysis", {"question": "公司没签劳动合同，还拖欠工资",
                           "slots": {"monthly_wage": 12000}}),
        ("case_decision", {"question": "公司拖欠工资，能要回多少", "limit": 3}),
    ]
    bad = 0
    for name, args in probes:
        try:
            out = call_tool(name, args, idx)
            n = len(json.dumps(out, ensure_ascii=False, default=str))
            print(f"  [OK] {name} → {n} 字符")
        except Exception as e:                          # noqa: BLE001
            bad += 1
            print(f"  [FAIL] {name} → {type(e).__name__}: {e}")
    print("自检通过" if not bad else f"自检 {bad} 项失败")
    return 0 if not bad else 1


def main() -> int:
    if "--self-check" in sys.argv[1:]:
        return self_check()
    return serve()


if __name__ == "__main__":
    sys.exit(main())
