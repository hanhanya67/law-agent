# -*- coding: utf-8 -*-
"""联网「开启路径」端到端探针：真起服务 + 本机回环数据源，一键可复跑。

为什么单独留这个脚本：`test_network.py` 用本机回环验的是**模块层**链路，
`check_ui.py` 在默认配置下验的是**「不联网」**；两者都没覆盖
「把开关全打开之后，服务层真的能把 external=1 透传、拿回线索、
并且不污染本地结论」这一段。而这段恰恰是「联网功能补全了没有」的证明。

全程不碰外网、不需要 key、不花钱：
  · 本机 ThreadingHTTPServer 冒充外部检索源，返回约定 JSON；
  · 主服务用 LAW_NET_MODE=online 强制在线（不依赖真实 DNS/外网）；
  · 临时库 + 回放模式，跑完即弃。

跑法：
    ./.venv/Scripts/python.exe scripts/probe_network_live.py
失败会打印断言并以非 0 退出。
"""
from __future__ import annotations

import json
import re
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from _client import Client            # noqa: E402
from _svc import start_service, stop_service   # noqa: E402

TOTAL = 0
FAILED: list[str] = []


def ok(name: str, cond: bool, detail: str = "") -> None:
    global TOTAL
    TOTAL += 1
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}"
          + (f"   {detail}" if detail else ""))
    if not cond:
        FAILED.append(name)


# 第三条故意带「胜率 73% / 建议申诉」，用来证明外部文案过滤在服务层也生效。
PAYLOAD = {"items": [
    {"title": "人社部：劳动人事争议典型案例（外部线索）",
     "url": "https://www.mohrss.gov.cn/xx/1",
     "snippet": "官方页面，供进一步查证。"},
    {"title": "最高法：劳动争议裁判要点索引",
     "url": "https://www.court.gov.cn/xx/2",
     "snippet": "公开资料索引。"},
    {"title": "这条该被丢掉：胜率 73%",
     "url": "https://example.com/x", "snippet": "建议申诉，包赢。"},
]}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:                              # noqa: N802
        body = json.dumps(PAYLOAD, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:                  # noqa: D102
        pass


def main() -> int:
    print("联网开启路径端到端探针（真服务 + 本机回环数据源 / 强制回放）")
    print("=" * 62)
    fixture = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = int(fixture.server_address[1])
    threading.Thread(target=fixture.serve_forever, name="fixture",
                     daemon=True).start()

    db = Path(tempfile.mkdtemp()) / "probe.db"
    env = {"LAW_DB_PATH": str(db), "ADMIN_PASSWORD": "Probe#LiveNet",
           "DEEPSEEK_API_KEY": "", "CASE_SEARCH_ADAPTER": "1",
           "LAW_CASE_SEARCH_URL": f"http://127.0.0.1:{port}/s?q={{q}}",
           "LAW_NET_MODE": "online"}
    svc, base = start_service(env=env)
    try:
        c = Client(base)
        ok("登录临时账号", bool(c.login("admin", "Probe#LiveNet")))
        _, nw = c.get("/api/network")
        ok("配置开关与网络门控都生效",
           nw["network"]["online"] is True
           and nw["external_search_enabled"] is True, str(nw["network"]["mode"]))

        q = "没签劳动合同，还拖欠两个月工资"
        st, d = c.post("/api/case/decision", {"question": q, "external": 1})
        ext = d.get("scope", {}).get("external", {})
        src = d.get("scope", {}).get("source", {})
        ok("external=1 真的拿到外部线索",
           st == 200 and ext.get("status") == "ok" and len(ext.get("items", [])) == 2,
           str(ext.get("status")))
        ok("含胜率/建议申诉的条目被服务层整条丢掉",
           ext.get("filtered") == 1
           and all("73" not in x["title"] for x in ext.get("items", [])),
           str(ext.get("filtered")))
        ok("每条线索都标未经本地核验",
           all(x.get("unverified") is True for x in ext.get("items", [])))
        ok("来源如实改成 external 且未误标降级",
           "外部检索" in str(src.get("label"))
           and src.get("web_enabled") is True and src.get("degraded") is False,
           str(src.get("label")))

        ok("顶层仍是四键", set(d) == {"scope", "similar", "cost_benefit", "policy"},
           str(sorted(d)))
        ids = [x.get("case_id") for x in d["similar"]["items"]]
        ok("相似案件仍是本地已核验案例（没被外部结果挤掉）",
           len(ids) == 4 and all(str(i).startswith("B") for i in ids), str(ids))
        blob = json.dumps(d, ensure_ascii=False)
        from app.costs import bad_percentages as _bad_pct
        ok("整份决策卡无百分比胜率", not _bad_pct(blob), str(_bad_pct(blob)[:2]))
        ok("整份决策卡不出现建议/不建议申诉",
           not re.search(r"(不?建议|应当|不该)(申诉|起诉|仲裁)", blob))
        ok("成本收益金额行仍留空", "＿＿＿＿" in blob)

        _, d0 = c.post("/api/case/decision", {"question": q})
        ok("同一服务不带 external 就不联网",
           d0["scope"]["external"]["status"] == "disabled"
           and d0["scope"]["external"]["items"] == [],
           str(d0["scope"]["external"]["status"]))
        _, ex = c.post("/api/case/extras", {"question": q, "external": 1})
        ok("整包接口与独立接口的 decision 逐字段一致", ex["decision"] == d)
        ok("整包接口仍是五块",
           {"timeline", "claims", "evidence_check", "decision", "persona"}
           <= set(ex), str(sorted(ex)))
    finally:
        stop_service(svc)
        fixture.shutdown()
        fixture.server_close()

    print("=" * 62)
    if FAILED:
        print(f"{TOTAL} 项：{TOTAL - len(FAILED)} 通过，{len(FAILED)} 失败")
        for f in FAILED:
            print("  -", f)
        return 1
    print(f"{TOTAL} 项全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
