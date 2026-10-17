# -*- coding: utf-8 -*-
"""腾讯云文字识别（OCR）客户端 —— 纯标准库实现，不依赖官方 SDK。

设计要点：
1. 只用 Python 标准库（hmac / hashlib / urllib）实现 TC3-HMAC-SHA256 签名，
   免去安装 ``tencentcloud-sdk-python`` 的麻烦，和本项目「零 Web 依赖」的
   风格保持一致。
2. 输出结构与本地 RapidOCR 完全兼容（``[(box, text, score), ...]``，其中
   ``box`` 是 4 个 ``[x, y]`` 顶点，``score`` 为 0~1），这样上层
   ``ocr_fallback`` 的版面重排代码一行都不用改。
3. 云端要求单张图片 base64 后 < 7MB，这里会自动等比缩放 + JPEG 压缩，
   并返回坐标还原系数，把返回坐标换算回原始渲染像素空间。

配置来源（优先级从高到低）：
   环境变量 TENCENT_SECRET_ID / TENCENT_SECRET_KEY / TENCENT_REGION
   同目录下的 ``tencent_config.json``

``tencent_config.json`` 示例::

    {
      "enabled": true,
      "secret_id": "AKIDxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
      "secret_key": "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
      "region": "ap-guangzhou",
      "action": "GeneralAccurateOCR"
    }

支持的接口（action）：
    GeneralAccurateOCR      通用文字识别（高精度版）—— 默认，中英混排+扫描件
    GeneralBasicOCR         通用印刷体识别 —— 最便宜
    GeneralHandwritingOCR   通用手写体识别 —— 手写笔记专用
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json
import os
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

HOST = "ocr.tencentcloudapi.com"
SERVICE = "ocr"
VERSION = "2018-11-19"
DEFAULT_REGION = "ap-guangzhou"

CONFIG_FILE = Path(__file__).resolve().parent / "tencent_config.json"

# 可用接口 → 中文名（供界面展示）
ACTIONS: Dict[str, str] = {
    "GeneralAccurateOCR": "通用文字识别（高精度版）",
    "GeneralBasicOCR": "通用印刷体识别",
    "GeneralHandwritingOCR": "通用手写体识别",
}
DEFAULT_ACTION = "GeneralAccurateOCR"

# 单张图片上传上限（腾讯云要求 base64 前 < 7MB，这里留点余量）
MAX_UPLOAD_BYTES = 6_500_000
# 图片最长边上限（太大既慢又没收益）
MAX_UPLOAD_SIDE = 2600
# 这些错误码值得重试（限频、服务瞬时不可用）
RETRY_CODES = {"RequestLimitExceeded", "InternalError", "FailedOperation.ServiceIsolate"}
MAX_RETRY = 3

_lock = threading.Lock()
_cache: Optional[Tuple[float, Dict[str, Any]]] = None


# --------------------------------------------------------------------------
# 配置
# --------------------------------------------------------------------------
def _read_config_file() -> Dict[str, Any]:
    if not CONFIG_FILE.is_file():
        return {}
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def load_config(force: bool = False) -> Dict[str, Any]:
    """读取配置（带 mtime 缓存，改了文件不用重启服务）。"""
    global _cache
    try:
        mtime = CONFIG_FILE.stat().st_mtime if CONFIG_FILE.is_file() else 0.0
    except OSError:
        mtime = 0.0

    with _lock:
        if not force and _cache is not None and _cache[0] == mtime:
            return _cache[1]

        raw = _read_config_file()
        action = str(raw.get("action") or DEFAULT_ACTION).strip()
        merged: Dict[str, Any] = {
            "enabled": bool(raw.get("enabled", True)),
            "secret_id": (
                os.environ.get("TENCENT_SECRET_ID")
                or str(raw.get("secret_id") or "").strip()
            ),
            "secret_key": (
                os.environ.get("TENCENT_SECRET_KEY")
                or str(raw.get("secret_key") or "").strip()
            ),
            "region": (
                os.environ.get("TENCENT_REGION")
                or str(raw.get("region") or DEFAULT_REGION).strip()
            ),
            "action": action if action in ACTIONS else DEFAULT_ACTION,
        }
        _cache = (mtime, merged)
        return merged


def has_credentials() -> bool:
    cfg = load_config()
    return bool(cfg["secret_id"] and cfg["secret_key"])


def is_enabled() -> bool:
    """是否应当走云端：开关打开 + 两个密钥都填了。"""
    cfg = load_config()
    return bool(cfg["enabled"] and cfg["secret_id"] and cfg["secret_key"])


def describe() -> Dict[str, Any]:
    """给界面/接口用的状态描述。"""
    cfg = load_config()
    if not CONFIG_FILE.is_file() and not os.environ.get("TENCENT_SECRET_ID"):
        state = "no-config"
    elif not cfg["enabled"]:
        state = "disabled"
    elif not (cfg["secret_id"] and cfg["secret_key"]):
        state = "no-key"
    else:
        state = "ready"

    action = cfg["action"]
    return {
        "state": state,
        "region": cfg["region"],
        "action": action,
        "api_name": ACTIONS.get(action, action),
        "config_file": str(CONFIG_FILE),
    }


# --------------------------------------------------------------------------
# TC3-HMAC-SHA256 签名
# --------------------------------------------------------------------------
def _sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _hmac_sha256(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _authorization(
    secret_id: str, secret_key: str, body: str, timestamp: int
) -> str:
    """按腾讯云 TC3 规范生成 Authorization 头。

    注意：region 只放在 X-TC-Region 头里，不参与签名计算。
    """
    date = datetime.fromtimestamp(timestamp, tz=timezone.utc).strftime("%Y-%m-%d")
    content_type = "application/json; charset=utf-8"

    canonical_headers = f"content-type:{content_type}\nhost:{HOST}\n"
    signed_headers = "content-type;host"
    canonical_request = "\n".join(
        ["POST", "/", "", canonical_headers, signed_headers, _sha256_hex(body)]
    )

    scope = f"{date}/{SERVICE}/tc3_request"
    string_to_sign = "\n".join(
        ["TC3-HMAC-SHA256", str(timestamp), scope, _sha256_hex(canonical_request)]
    )

    k_date = _hmac_sha256(("TC3" + secret_key).encode("utf-8"), date)
    k_service = _hmac_sha256(k_date, SERVICE)
    k_signing = _hmac_sha256(k_service, "tc3_request")
    signature = hmac.new(
        k_signing, string_to_sign.encode("utf-8"), hashlib.sha256
    ).hexdigest()

    return (
        f"TC3-HMAC-SHA256 Credential={secret_id}/{scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )


def _extract_error(payload: Dict[str, Any]) -> Tuple[str, str]:
    err = (payload.get("Response") or {}).get("Error") or {}
    return str(err.get("Code") or ""), str(err.get("Message") or "")


def _request(action: str, payload: Dict[str, Any], cfg: Dict[str, Any]) -> Dict[str, Any]:
    """发一次 OCR 请求，返回 Response 字典；失败抛 RuntimeError。"""
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    last_error: Optional[str] = None

    for attempt in range(1, MAX_RETRY + 1):
        timestamp = int(time.time())
        request = urllib.request.Request(
            f"https://{HOST}/",
            data=body.encode("utf-8"),
            headers={
                "Authorization": _authorization(
                    cfg["secret_id"], cfg["secret_key"], body, timestamp
                ),
                "Content-Type": "application/json; charset=utf-8",
                "Host": HOST,
                "X-TC-Action": action,
                "X-TC-Timestamp": str(timestamp),
                "X-TC-Version": VERSION,
                "X-TC-Region": cfg["region"],
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            try:
                code, msg = _extract_error(json.loads(detail))
            except (ValueError, KeyError):
                code, msg = f"HTTP {exc.code}", detail[:200]
            last_error = f"{code}: {msg}"
            if code in RETRY_CODES and attempt < MAX_RETRY:
                time.sleep(attempt)
                continue
            raise RuntimeError(f"腾讯云 OCR 调用失败（{code}）：{msg}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = f"网络错误：{exc}"
            if attempt < MAX_RETRY:
                time.sleep(attempt)
                continue
            raise RuntimeError(last_error) from exc

        code, msg = _extract_error(data)
        if code:
            last_error = f"{code}: {msg}"
            if code in RETRY_CODES and attempt < MAX_RETRY:
                time.sleep(attempt)
                continue
            raise RuntimeError(f"腾讯云 OCR 调用失败（{code}）：{msg}")

        return data.get("Response") or {}

    raise RuntimeError(last_error or "未知错误")


# --------------------------------------------------------------------------
# 图片预处理
# --------------------------------------------------------------------------
def prepare_image(image_bytes: bytes) -> Tuple[bytes, float]:
    """把图片压到云端能接受的大小。

    Returns:
        (上传字节, 坐标还原系数)。系数 = 原图像素 / 上传图像素，
        用云端返回坐标乘上它即可换算回原始渲染尺寸。
    """
    try:
        from PIL import Image
    except Exception:  # noqa: BLE001
        return image_bytes, 1.0

    try:
        img = Image.open(io.BytesIO(image_bytes))
        img.load()
    except Exception:  # noqa: BLE001
        return image_bytes, 1.0

    width, height = img.size
    # 又不大又够小：原样发送，避免二次压缩伤画质
    if len(image_bytes) <= MAX_UPLOAD_BYTES and max(width, height) <= MAX_UPLOAD_SIDE:
        return image_bytes, 1.0

    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")

    sent_w, sent_h = width, height
    longest = max(width, height)
    if longest > MAX_UPLOAD_SIDE:
        ratio = MAX_UPLOAD_SIDE / float(longest)
        sent_w, sent_h = max(1, int(width * ratio)), max(1, int(height * ratio))
        img = img.resize((sent_w, sent_h), Image.LANCZOS)

    quality = 90
    data = b""
    while True:
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality, optimize=True)
        data = buf.getvalue()
        if len(data) <= MAX_UPLOAD_BYTES or quality <= 40:
            break
        quality -= 10

    return data, width / float(sent_w)


def _polygon_to_box(entry: Dict[str, Any], factor: float) -> Optional[List[List[float]]]:
    """把云端返回的多边形转成 4 个顶点（兼容多种 Polygon 结构）。"""
    raw = entry.get("Polygon")

    # 情况 1：Polygon 是坐标点列表，例如 [{"X":37,"Y":46}, {"X":407,"Y":46}, ...]
    # （GeneralHandwritingOCR 手写体接口返回这种结构）
    if isinstance(raw, list):
        pts: List[List[float]] = []
        for p in raw:
            try:
                x = float(p.get("X", 0))
                y = float(p.get("Y", 0))
            except (TypeError, ValueError, AttributeError):
                continue
            pts.append([x * factor, y * factor])
        if len(pts) >= 4:
            return pts[:4]
        return None

    # 情况 2：Polygon 是逗号分隔的数字字符串，如 "37,46,407,46,407,96,37,96"
    # （GeneralAccurateOCR 高精度接口返回这种结构）
    if raw:
        try:
            nums = [float(x) for x in str(raw).split(",")]
        except ValueError:
            nums = []
        if len(nums) >= 8:
            return [
                [nums[i] * factor, nums[i + 1] * factor] for i in range(0, 8, 2)
            ]

    # 情况 3：用 ItemPolygon（X/Y/Width/Height）兜底
    item = entry.get("ItemPolygon") or {}
    try:
        x = float(item.get("X", 0))
        y = float(item.get("Y", 0))
        w = float(item.get("Width", 0))
        h = float(item.get("Height", 0))
    except (TypeError, ValueError):
        return None
    if w > 0 and h > 0:
        return [
            [x * factor, y * factor],
            [(x + w) * factor, y * factor],
            [(x + w) * factor, (y + h) * factor],
            [x * factor, (y + h) * factor],
        ]
    return None


# --------------------------------------------------------------------------
# 对外入口
# --------------------------------------------------------------------------
def recognize(
    image_bytes: bytes, config: Optional[Dict[str, Any]] = None
) -> List[Tuple[List[List[float]], str, float]]:
    """识别一张图片，返回与 RapidOCR 兼容的结构。

    Raises:
        RuntimeError: 未配置密钥，或云端返回错误。
    """
    cfg = config or load_config()
    if not (cfg.get("secret_id") and cfg.get("secret_key")):
        raise RuntimeError("未配置腾讯云 SecretId / SecretKey")

    action = cfg.get("action") if cfg.get("action") in ACTIONS else DEFAULT_ACTION
    data, factor = prepare_image(image_bytes)

    response = _request(
        action, {"ImageBase64": base64.b64encode(data).decode("ascii")}, cfg
    )

    items: List[Tuple[List[List[float]], str, float]] = []
    for entry in response.get("TextDetections") or []:
        text = str(entry.get("DetectedText") or "").strip()
        if not text:
            continue
        box = _polygon_to_box(entry, factor)
        if box is None:
            continue
        try:
            score = float(entry.get("Confidence") or 0.0) / 100.0
        except (TypeError, ValueError):
            score = 1.0
        items.append((box, text, score))

    return items
