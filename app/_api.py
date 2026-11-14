# -*- coding: utf-8 -*-
"""最小 HTTP 客户端，直接对接 OpenAI 兼容接口（DeepSeek / 任何同格式服务）。

为什么不用官方 openai SDK
--------------------------
本机 Windows 的应用程序控制策略（AppLocker / WDAC）阻止了 jiter 的 .pyd
加载：

    ImportError: DLL load failed while importing jiter:
    应用程序控制策略已阻止此文件。

jiter 是 SDK 用来加速 JSON 解析的 Rust 扩展，属于硬依赖，绕不开。更麻烦的是
SDK 在 import 阶段不报错，只在**第一次真实调用**时炸——整轮评测因此静默降级
成回放模式，指标全是 0，从日志表面完全看不出是环境问题。

改用标准库 urllib 直接发请求：
  1. 零第三方依赖，不受应用程序控制策略影响
  2. 响应对象用 SimpleNamespace 对齐 SDK 结构，上层业务代码一行不用改
  3. 出问题时能看到真实的 HTTP 状态和响应体

设计约束
--------
只实现本项目的三个调用形态，不追求做通用 SDK：
  阻塞 + tools     决策，要拿到完整 tool_calls 才能执行
  流式 + 文本      生成，逐字推给前端
  阻塞 + json      抽取 / 定性，temperature=0
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from types import SimpleNamespace as NS
from typing import Any, Iterator

from . import health

# 推理模型可能思考很久，超时给足；由 Agent 的 MAX_AGENT_SECONDS 兜总时长
DEFAULT_TIMEOUT = 180


class APIError(RuntimeError):
    """HTTP 层错误。带上状态码，便于区分「认证失败」与「网络抖动」。"""

    def __init__(self, status: int, body: str):
        self.status = status
        self.body = body
        super().__init__(f"HTTP {status}: {body[:300]}")

    @property
    def fatal(self) -> bool:
        """认证/余额/参数类错误重试也没用，直接降级；5xx 与超时值得再试。"""
        return self.status in (400, 401, 402, 403, 404, 422)


def _wrap_choice(payload: dict, stream: bool) -> NS:
    msg = payload.get("message") or payload.get("delta") or {}
    tool_calls = msg.get("tool_calls")
    calls = None
    if tool_calls:
        calls = [NS(id=c.get("id", ""),
                    type="function",
                    function=NS(name=(c.get("function") or {}).get("name", ""),
                                arguments=(c.get("function") or {}).get("arguments", "{}")))
                 for c in tool_calls]
    node = NS(content=msg.get("content"),
              reasoning_content=msg.get("reasoning_content"),
              tool_calls=calls,
              role=msg.get("role"))
    # 非流式读 .message，流式读 .delta。指向同一对象，调用方怎么写都能取到。
    return NS(index=payload.get("index", 0), message=node, delta=node,
              finish_reason=payload.get("finish_reason"))


def _wrap(raw: dict, stream: bool) -> NS:
    choices = [_wrap_choice(c, stream) for c in raw.get("choices") or []]
    return NS(choices=choices, usage=raw.get("usage") or {},
              model=raw.get("model"), id=raw.get("id"))


class _Completions:
    def __init__(self, base_url: str, api_key: str, timeout: int = DEFAULT_TIMEOUT):
        self.url = base_url.rstrip("/") + "/chat/completions"
        self.key = api_key
        self.timeout = timeout

    def _request(self, body: dict) -> urllib.request.Request:
        return urllib.request.Request(
            self.url,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self.key}",
                     "Accept": "text/event-stream" if body.get("stream") else "application/json"},
            method="POST")

    def create(self, **kw: Any):
        """与 SDK 同签名。stream=True 返回迭代器，否则返回完整响应对象。"""
        # SDK 的 extra_body 是「原样并入请求体」，这里照做：
        # thinking / reasoning_effort 这类非标准字段都从这里进去。
        extra = kw.pop("extra_body", None)
        if extra:
            kw.update(extra)
        stream = bool(kw.get("stream"))

        try:
            resp = urllib.request.urlopen(self._request(kw), timeout=self.timeout)
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "ignore")
            # 只有这一处知道「是 402 还是 401」，所以状态记录放在这里，
            # 上层不用各自去猜。见 app/health.py。
            health.note_error(e.code, body)
            raise APIError(e.code, body) from None
        except urllib.error.URLError as e:
            health.note_error(kind="network", msg=str(e.reason))
            raise ConnectionError(f"网络不可达：{e.reason}") from None
        except TimeoutError:
            health.note_error(kind="network", msg="调用超时")
            raise

        if not stream:
            raw = json.loads(resp.read().decode("utf-8"))
            if "error" in raw:
                health.note_error(500, json.dumps(raw["error"], ensure_ascii=False))
                raise APIError(500, json.dumps(raw["error"], ensure_ascii=False))
            health.note_ok()
            return _wrap(raw, stream=False)

        health.note_ok()
        return self._iter_sse(resp)

    @staticmethod
    def _iter_sse(resp) -> Iterator[NS]:
        """逐行解析 SSE。只取 data: 行，[DONE] 结束。

        必须 finally 关闭：生成器被提前丢弃（比如上层 break 或抛异常）时，
        不关连接会让 socket 一直挂着，长跑批量评测会积累到端口耗尽。
        """
        try:
            for line in resp:
                text = line.decode("utf-8", "ignore").strip()
                if not text or not text.startswith("data:"):
                    continue
                data = text[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    raw = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if "error" in raw:
                    raise APIError(500, json.dumps(raw["error"], ensure_ascii=False))
                yield _wrap(raw, stream=True)
        finally:
            resp.close()


class MiniClient:
    """只有 chat.completions.create 一个入口，故意保持极窄。"""

    def __init__(self, api_key: str, base_url: str, timeout: int = DEFAULT_TIMEOUT):
        self.chat = NS(completions=_Completions(base_url, api_key, timeout))
