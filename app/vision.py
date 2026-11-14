"""图片证据：把用户上传的截图变成案情槽位。

为什么值得单开一个模块，而不是在路由里直接调 LLM：

1. **校验必须在调用模型之前做完。** 手机上随手拍的一张图常有 5–10 MB，
   base64 之后膨胀三分之一，撞上请求体 48 MiB 上限时，模型返回的错误信息
   对用户毫无意义。先在自己这边拦掉，才能说清「图太大了」。

2. **图片格式必须按文件内容判断，不能看扩展名。** 微信/QQ 保存下来的图
   经常是后缀 .jpg、内容却是 PNG。按后缀去拼 data URL 里的 mime，
   模型会 400 —— 而用户看到的只是「识别失败」，完全不知道该怎么改。

3. **「抽到了什么」和「这张图能不能信」是两件事。** 前者是 fields，
   后者是 unreadable 和 confidence。只把字段交出去，等于替用户做了
   「这张图很清楚」的假设，而这个假设错了会一路算到赔偿金额上。
"""

from __future__ import annotations

import base64
from typing import Any

from .schemas import SLOT_LABELS

# 单张上限。8 MB 足够一张 4000×3000 的手机照片（JPEG 通常 2–4 MB），
# 又远低于「base64 后撞上 48 MiB 请求体上限」的量级。
MAX_BYTES = 8 * 1024 * 1024

# 只支持这四种。DeepSeek 文档写明支持 JPEG / PNG / GIF / WebP，
# 且**按文件内容识别格式**，不看文件名和声明的 MIME。
_MAGIC: tuple[tuple[bytes, str, str], ...] = (
    (b"\xff\xd8\xff", "image/jpeg", "JPEG"),
    (b"\x89PNG\r\n\x1a\n", "image/png", "PNG"),
    (b"GIF87a", "image/gif", "GIF"),
    (b"GIF89a", "image/gif", "GIF"),
)

DOC_LABELS = {
    "pay_slip": "工资条 / 银行流水",
    "contract": "劳动合同",
    "dismissal": "辞退通知 / 解除证明",
    "attendance": "考勤 / 排班记录",
    "chat": "聊天记录截图",
    "other": "其他材料",
}


class ImageError(ValueError):
    """图片本身有问题（太大 / 格式不支持）。调用方据此给 400，不要重试。"""


def sniff_image(data: bytes) -> str:
    """按文件头判断 MIME。认不出来就抛 ImageError，不要把未知字节交给模型。"""
    if not data:
        raise ImageError("文件是空的")
    for magic, mime, _name in _MAGIC:
        if data.startswith(magic):
            return mime
    # WebP 的头是 RIFF....WEBP，中间 4 字节是长度，不能整段前缀匹配
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    raise ImageError("只支持 JPEG / PNG / GIF / WebP 四种图片格式")


def sniff_name(data: bytes) -> str:
    """给用户看的格式名。错误提示里说「HEIC」比说「不支持的格式」有用得多。"""
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "WebP"
    for magic, _mime, name in _MAGIC:
        if data.startswith(magic):
            return name
    # HEIC 是 iPhone 默认格式，也是最常见的「传上来认不出」的那一种，
    # 单独点出来，用户才知道要在相册里导成 JPEG。
    if len(data) >= 12 and data[4:8] == b"ftyp" and data[8:12] in (
            b"heic", b"heix", b"hevc", b"mif1", b"msf1"):
        return "HEIC"
    return "未知格式"


def extract(data: bytes, llm: Any) -> dict[str, Any]:
    """读图 → 槽位字段。抛 ImageError 表示图本身不合规。"""
    if not data:
        raise ImageError("没有收到图片内容")
    if len(data) > MAX_BYTES:
        raise ImageError(
            f"图片 {len(data) / 1024 / 1024:.1f} MB，超过 "
            f"{MAX_BYTES // 1024 // 1024} MB 上限，请压缩后再传")
    mime = sniff_image(data)        # 认不出就抛，不猜
    b64 = base64.b64encode(data).decode("ascii")

    result = llm.extract_from_image(b64, mime)

    fields: dict[str, dict[str, Any]] = {}
    raw_fields = result.get("fields") or {}
    # 按 SLOT_LABELS 的顺序排，不按模型返回的顺序。模型每次给的键序不一样，
    # 同一张图重读一次，「月工资」就从第三行跑到第一行——用户会以为读出了
    # 别的结果。固定顺序还有一个好处：这一屏永远和案情档案页的字段顺序一致。
    for key in SLOT_LABELS:
        item = raw_fields.get(key)
        if not item:
            continue
        fields[key] = {
            "key": key,
            "label": SLOT_LABELS.get(key, key),
            "value": item.get("value"),
            "quote": item.get("quote") or "",
            "confidence": item.get("confidence") or "low",
        }

    doc_type = result.get("doc_type") or "other"
    return {
        "ok": True,
        "doc_type": doc_type,
        "doc_label": DOC_LABELS.get(doc_type, DOC_LABELS["other"]),
        "fields": fields,
        # 低于 high 的一律标「请核对」。不分开的话，一眼扫过去会以为
        # 8 个字段都同样可靠，而其中可能有一个是模型猜的。
        "need_check": [k for k, v in fields.items()
                       if v["confidence"] != "high"],
        "unreadable": result.get("unreadable") or [],
        "note": result.get("note") or "",
        # 一个字段都没抽到不是错误：可能是张无关的图，也可能是图太糊。
        # 交给前端说清楚，而不是让用户以为系统坏了。
        "empty": not fields,
    }


def to_patch(fields: dict[str, dict[str, Any]], accepted: list[str]) -> dict[str, Any]:
    """把用户勾选确认过的字段拼成可以 merge 进 CaseSlots 的 patch。

    **只有 accepted 里的字段会进去。** 这一步是整条链路的闸门：
    模型读出来多少不算数，用户勾了多少才算。少勾一个字段，最坏结果是
    追问那一个；多进一个错的，赔偿金额直接错，而没人会发现。
    """
    return {k: v["value"] for k, v in fields.items()
            if k in accepted and v.get("value") is not None}
