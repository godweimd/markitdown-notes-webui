# -*- coding: utf-8 -*-
"""智谱 GLM 视觉理解（VL）客户端 —— 手写笔记 / 公式 → LaTeX 的兜底识别。

为什么需要这个模块：
    通用 OCR（本地 RapidOCR / 腾讯云）只能“认字”，碰到数学公式就会把
    分式、根号、上下标、矩阵拆成一堆毫无意义的散字符。要让公式变成**严谨、
    可再编辑的 LaTeX 代码**，得靠多模态大模型“看懂”公式结构再重写出来。
    智谱 GLM-4V / GLM-4.5V 就是干这个的：把整页图片丢进去，直接返回
    一段带 ``$...$`` / ``$$...$$`` 的 Markdown。

设计要点：
1. 只用 Python 标准库（urllib / json / base64），和本项目「零 Web 依赖」风格
   一致，不需要装 ``zhipuai`` 官方 SDK。
2. 与 ``tencent_ocr`` 不同，VL 模型返回的是**整页 Markdown 文本**而不是
   ``(box, text, score)`` 文字块 —— 它自带版面理解能力，不需要上层再重排。
   所以 ``ocr_fallback`` 里 vl 模式走的是另一条分支（见 ``_run_vl``）。
3. 接口是 OpenAI 兼容的 ``/chat/completions``，鉴权用 Bearer Token。

配置来源（优先级从高到低）：
    环境变量 ``ZHIPU_API_KEY`` / ``ZHIPU_MODEL``
    同目录下的 ``zhipu_config.json``

``zhipu_config.json`` 示例::

    {
      "enabled": true,
      "api_key": "xxxxxxxx.xxxxxxxxxxxxxxxxxxxxxxxx",
      "model": "glm-4v-flash",
      "prompt": ""            // 留空则用内置的数学笔记转写提示词
    }

可选模型（按能力/价格从低到高）：
    glm-4v-flash     免费，适合试跑；公式能力一般
    glm-4v-plus      付费，公式/手写识别明显更好
    glm-4.5v         最新旗舰，多步推理最强，价格最高
"""

from __future__ import annotations

import base64
import io
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

API_URL = "https://open.bigmodel.cn/api/paas/v4/chat/completions"
DEFAULT_MODEL = "glm-4v-flash"

# 界面下拉里可选的模型 → 中文说明
MODELS: Dict[str, str] = {
    "glm-4v-flash": "GLM-4V-Flash（免费）",
    "glm-4v-plus": "GLM-4V-Plus（付费·公式更强）",
    "glm-4.5v": "GLM-4.5V（旗舰·最强）",
}

# 各模型 max_tokens 上限不同。glm-4v-flash 免费，输出被硬限在 1024；付费模型宽松得多。
# 这里给出各模型的**真实上限**；万一仍撞到上限，_request() 会按接口提示自动收窄重试。
MODEL_MAX_TOKENS: Dict[str, int] = {
    "glm-4v-flash": 1024,
    "glm-4v-plus": 8192,
    "glm-4.5v": 16384,
}

# 输出撞到上限（finish_reason=length）时，自动让模型「接着写」的最大轮数。
# 一页手写笔记用免费 glm-4v-flash（1024 上限）通常 1~2 轮就能写完；
# 给到 3 轮（最多 4 次请求）足以覆盖绝大多数长页面。
MAX_CONTINUATIONS = 3

# 续写指令：明确要求「不重复、不解释、直接接」，避免模型复述或重排
_CONTINUE_PROMPT = (
    "你上一条回复因为长度限制被截断了。请**从被截断的位置继续**输出剩余内容：\n"
    "1. 不要重复已经输出过的任何文字；\n"
    "2. 不要重新写标题、不要加「继续」「以下是剩余内容」之类的前言或说明；\n"
    "3. 直接接着上文写下去，保持同样的 Markdown 与 LaTeX 格式。\n"
    "如果内容已经全部输出完毕，请只回复：<END>"
)

CONFIG_FILE = Path(__file__).resolve().parent / "zhipu_config.json"

# 单张图上传上限（智谱要求 base64 前 < 5MB，这里留余量）
MAX_UPLOAD_BYTES = 4_500_000
# 图片最长边上限。手写笔记的字够大，2048 已经足够模型看清
MAX_UPLOAD_SIDE = 2048

# 默认提示词：把整页笔记转写成 Markdown，公式一律 LaTeX
DEFAULT_PROMPT = (
    "你是一位严谨的数学笔记数字化助手。请把这张图片里的内容完整转写成 Markdown，"
    "严格遵守以下规则：\n"
    "1. 所有数学公式、符号一律输出 LaTeX 代码：行内公式用 $...$ 包裹，"
    "独立成行的公式用 $$...$$ 包裹。\n"
    "2. 保持原文的结构：标题用 #/##，分条用 - 或数字列表，段落之间空一行。\n"
    "3. 忠实转写，不要解释、不要总结、不要补充图片里没有的内容。\n"
    "4. 手写体请结合上下文推断正确符号（例如把潦草的 2 和 z、1 和 l 按语境区分）。\n"
    "5. 遇到示意图 / 几何图，用一句话描述并另起一段标注「> [图] 描述」。\n"
    "6. 只输出转写结果本身，不要任何前言、结尾说明，也不要用代码块围栏。"
)

# 图片方向：手写照片常见「拍倒了」（旋转 180°），先让模型判朝向再摆正。
#   "auto"        = 自动判断（默认）
#   0/90/180/270  = 手动指定「顺时针」旋转角度
ROTATE_OPTIONS = ("auto", 0, 90, 180, 270)

# 判断朝向的提示词。**必须是这种「列出四个选项」的问法**——实测若改成
# 「正 / 上下颠倒」二选一，模型会一律答「正常」（连明显倒置的图也答错）；
# 而列出四选项时，它对「上下颠倒」这一档判得很准（10/10）。
# 注意：90/270 这种侧躺角度模型判不准，所以 detect_rotation() 先用长宽比把
# 图片摆成竖版（横版→先转 270°），再用这个提示词只判「正 / 倒置」。
_ORIENT_PROMPT = (
    "判断这张照片里手写文字的朝向。只回答以下四个选项之一，不要任何解释："
    "正常 / 倒置180度 / 顺时针90度 / 逆时针90度"
)

# 值得重试的 HTTP 状态码（限频、服务瞬时错误）
RETRY_STATUS = {429, 500, 502, 503, 504}
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
        model = (
            os.environ.get("ZHIPU_MODEL")
            or str(raw.get("model") or DEFAULT_MODEL)
        ).strip()
        try:
            max_tokens = int(raw.get("max_tokens") or 0)
        except (TypeError, ValueError):
            max_tokens = 0
        rotate = raw.get("rotate", "auto")
        if isinstance(rotate, str) and rotate.strip().lower() != "auto":
            try:
                rotate = int(rotate)
            except ValueError:
                rotate = "auto"
        if rotate != "auto":
            rotate = int(rotate) % 360 if int(rotate) % 360 in (0, 90, 180, 270) else "auto"
        merged: Dict[str, Any] = {
            "enabled": bool(raw.get("enabled", True)),
            "api_key": (
                os.environ.get("ZHIPU_API_KEY")
                or str(raw.get("api_key") or "").strip()
            ),
            "model": model if model in MODELS else DEFAULT_MODEL,
            "prompt": str(raw.get("prompt") or "").strip() or DEFAULT_PROMPT,
            # 0 表示按模型自动选择上限（见 MODEL_MAX_TOKENS）
            "max_tokens": max_tokens if max_tokens > 0 else 0,
            # "auto" 自动判朝向；0/90/180/270 手动指定顺时针旋转角度
            "rotate": rotate,
        }
        _cache = (mtime, merged)
        return merged


def has_credentials() -> bool:
    return bool(load_config()["api_key"])


def is_enabled() -> bool:
    """是否应当走智谱 VL：开关打开 + 填了 API Key。"""
    cfg = load_config()
    return bool(cfg["enabled"] and cfg["api_key"])


def describe() -> Dict[str, Any]:
    """给界面/接口用的状态描述。"""
    cfg = load_config()
    if not CONFIG_FILE.is_file() and not os.environ.get("ZHIPU_API_KEY"):
        state = "no-config"
    elif not cfg["enabled"]:
        state = "disabled"
    elif not cfg["api_key"]:
        state = "no-key"
    else:
        state = "ready"

    return {
        "state": state,
        "model": cfg["model"],
        "model_name": MODELS.get(cfg["model"], cfg["model"]),
        "rotate": cfg["rotate"],
        "config_file": str(CONFIG_FILE),
        # 前端「模型」下拉框用的候选列表（按价格/能力从低到高）
        "models": [
            {"value": m, "label": MODELS[m]}
            for m in ("glm-4v-flash", "glm-4v-plus", "glm-4.5v")
        ],
    }


# --------------------------------------------------------------------------
# 图片预处理
# --------------------------------------------------------------------------
def _rotate_bytes(image_bytes: bytes, deg: int) -> bytes:
    """把图片**顺时针**旋转 deg 度（0/90/180/270），用于把倒置的手写照片摆正。"""
    if not deg:
        return image_bytes
    try:
        from PIL import Image

        img = Image.open(io.BytesIO(image_bytes))
        img.load()
        # PIL 的 rotate 是逆时针，这里统一按「顺时针 deg 度」处理
        img = img.rotate(-int(deg) % 360, expand=True)
        if img.mode != "RGB":
            img = img.convert("RGB")
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=92)
        return buf.getvalue()
    except Exception:  # noqa: BLE001
        return image_bytes


def prepare_image(image_bytes: bytes) -> bytes:
    """把图片压到智谱能接受的长宽与体积（过大既慢又浪费 token）。"""
    try:
        from PIL import Image
    except Exception:  # noqa: BLE001
        return image_bytes

    try:
        img = Image.open(io.BytesIO(image_bytes))
        img.load()
    except Exception:  # noqa: BLE001
        return image_bytes

    width, height = img.size
    if len(image_bytes) <= MAX_UPLOAD_BYTES and max(width, height) <= MAX_UPLOAD_SIDE:
        return image_bytes

    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")

    longest = max(width, height)
    if longest > MAX_UPLOAD_SIDE:
        ratio = MAX_UPLOAD_SIDE / float(longest)
        img = img.resize(
            (max(1, int(width * ratio)), max(1, int(height * ratio))), Image.LANCZOS
        )

    quality = 90
    data = b""
    while True:
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality, optimize=True)
        data = buf.getvalue()
        if len(data) <= MAX_UPLOAD_BYTES or quality <= 40:
            break
        quality -= 10
    return data


# --------------------------------------------------------------------------
# 调用
# --------------------------------------------------------------------------
def _request(cfg: Dict[str, Any], messages: List[Dict[str, Any]], max_tokens: int) -> Dict[str, Any]:
    """发一次 chat/completions 请求，返回解析后的 JSON（含重试与 max_tokens 自动收窄）。"""
    payload: Dict[str, Any] = {
        "model": cfg["model"],
        "messages": messages,
        # 转写任务要稳定、忠实，温度调低
        "temperature": 0.1,
        "max_tokens": max_tokens,
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    last_error: Optional[str] = None
    for attempt in range(1, MAX_RETRY + 1):
        request = urllib.request.Request(
            API_URL,
            data=body,
            headers={
                "Authorization": f"Bearer {cfg['api_key']}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                raw = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            last_error = f"HTTP {exc.code}：{detail[:300]}"

            # max_tokens 超限（错误码 1210）：按接口提示的上限自动收窄后重试
            if exc.code == 400 and "max_tokens" in detail:
                m = re.search(r"\[1,(\d+)\]", detail)
                if m:
                    allowed = int(m.group(1))
                    if payload.get("max_tokens", 0) > allowed:
                        payload["max_tokens"] = allowed
                        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                        continue

            if exc.code in RETRY_STATUS and attempt < MAX_RETRY:
                time.sleep(attempt * 1.5)
                continue
            raise RuntimeError(f"智谱 GLM 调用失败（{last_error}）") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = f"网络错误：{exc}"
            if attempt < MAX_RETRY:
                time.sleep(attempt * 1.5)
                continue
            raise RuntimeError(last_error) from exc

        # 智谱错误也可能以 200 + error 字段返回
        if isinstance(raw, dict) and raw.get("error"):
            raise RuntimeError(f"智谱 GLM 返回错误：{raw.get('error')}")
        return raw

    raise RuntimeError(last_error or "未知错误")


def _ask(
    image_bytes: bytes, prompt: Optional[str], rotate_deg: int = 0, model: Optional[str] = None
) -> str:
    """真正发起 GLM 调用（内部使用）。``rotate_deg`` 为顺时针旋转角度。

    ``model`` 覆盖配置里的模型（前端「模型」下拉框切换用）；为 None 时用配置值。

    如果输出撞到模型上限（``finish_reason == "length"``），会自动把已输出的内容
    作为上下文发回去、要求模型「接着写」，最多续 ``MAX_CONTINUATIONS`` 轮，
    这样即使是输出上限只有 1024 的免费模型也能写完整页。
    """
    cfg = load_config()
    if not cfg["api_key"]:
        raise RuntimeError("未配置智谱 API Key（zhipu_config.json 或环境变量 ZHIPU_API_KEY）")

    # 允许调用方覆盖模型（仅限已知模型，非法值回退到配置值）
    if model and model in MODELS:
        cfg = dict(cfg, model=model)

    data = prepare_image(_rotate_bytes(image_bytes, rotate_deg))
    b64 = base64.b64encode(data).decode("ascii")

    # max_tokens 各模型上限不同（glm-4v-flash 仅 [1,1024]），按模型取默认上限
    max_tokens = cfg.get("max_tokens") or MODEL_MAX_TOKENS.get(cfg["model"], 1024)

    messages: List[Dict[str, Any]] = [
        {
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": b64}},
                {"type": "text", "text": prompt or cfg["prompt"]},
            ],
        }
    ]

    chunks: List[str] = []
    finish_reason: Optional[str] = None
    for _round in range(MAX_CONTINUATIONS + 1):
        raw = _request(cfg, messages, max_tokens)
        choices = raw.get("choices") or []
        if not choices:
            raise RuntimeError(f"智谱 GLM 返回异常：{str(raw)[:300]}")

        choice = choices[0]
        content = str((choice.get("message") or {}).get("content") or "")
        chunks.append(content)
        finish_reason = choice.get("finish_reason")

        if finish_reason != "length":
            break  # 正常结束

        # 撞到上限：把本轮输出作为 assistant 上下文，要求接着写
        messages.append({"role": "assistant", "content": content})
        messages.append({"role": "user", "content": _CONTINUE_PROMPT})

    merged = "".join(chunks)
    # 模型偶尔会吐出续写哨兵，去掉
    merged = merged.replace("<END>", "").replace("<END >", "")
    text = _normalize_math(_strip_fences(merged))

    # 续了 MAX_CONTINUATIONS 轮仍是 length → 确实太长，留个说明
    if finish_reason == "length":
        text += (
            f"\n\n<!-- 注意：已自动续写 {MAX_CONTINUATIONS} 轮仍未输出完整，"
            "该页内容过长。建议把 zhipu_config.json 的 model 改为 glm-4.5v 后重试 -->"
        )
    return text


def detect_rotation(image_bytes: bytes) -> int:
    """判断图片需要**顺时针**旋转多少度才能摆正，返回 0 / 90 / 180 / 270。

    **两段式策略**（比让模型一次判 4 个方向可靠得多）：

    1. **长宽比定「轴」**：笔记本页面本身是竖版，所以照片是横版（宽 > 高）就说明
       页面被转了 90°/270° —— 先顺时针转 270° 把它摆回竖版；竖版照片则不动。
    2. **模型只判 0/180**：摆成竖版后，剩下的只可能是「正」或「上下颠倒」，
       交给模型二选一（这一档实测很稳）。

    失败或无法判断时返回 0（不旋转）。
    """
    base = 0
    try:
        from PIL import Image

        with Image.open(io.BytesIO(image_bytes)) as img:
            w, h = img.size
        if w > h:  # 横版 → 页面被转了 90/270，先转回竖版（若真值其实是 90°，
            base = 270  # 下面模型会判成「倒置180度」，再加 180 正好 = 90）
    except Exception:  # noqa: BLE001
        base = 0

    try:
        answer = _ask(image_bytes, _ORIENT_PROMPT, base)  # 先按 base 摆成竖版再问
    except Exception:  # noqa: BLE001
        return base

    if "倒置" in answer or "180" in answer:
        base += 180
    return base % 360


def resolve_rotation(image_bytes: bytes, rotate: Any = None) -> int:
    """把配置/参数里的 rotate 值解析成具体角度。

    ``None`` / ``"auto"`` → 调模型自动判断；数字则原样使用（收敛到 0/90/180/270）。
    """
    value = load_config()["rotate"] if rotate is None else rotate
    if isinstance(value, str) and value.strip().lower() == "auto":
        return detect_rotation(image_bytes)
    try:
        deg = int(value)
    except (TypeError, ValueError):
        return 0
    return deg % 360 if deg % 360 in (0, 90, 180, 270) else 0


def transcribe(
    image_bytes: bytes,
    prompt: Optional[str] = None,
    rotate: Any = None,
    model: Optional[str] = None,
) -> str:
    """把一张图片交给 GLM-4V，返回带 LaTeX 的 Markdown 文本。

    Args:
        prompt: 覆盖默认转写提示词。
        rotate: ``None`` 用配置里的 ``rotate``（默认 ``"auto"``）；也可传
            ``"auto"`` / ``0`` / ``90`` / ``180`` / ``270`` 覆盖。``"auto"``
            会先调一次模型判断朝向，把倒置的手写照片摆正后再转写。
        model: 覆盖配置里的模型（如 ``"glm-4.5v"``）；为 None 用配置值。

    Raises:
        RuntimeError: 未配置 Key，或接口返回错误。
    """
    deg = resolve_rotation(image_bytes, rotate)
    return _ask(image_bytes, prompt, deg, model=model)


def _strip_fences(text: str) -> str:
    """去掉模型偶尔多写的 ``` 代码围栏。"""
    t = text.strip()
    if t.startswith("```"):
        lines = t.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        t = "\n".join(lines).strip()
    return t


def _normalize_math(text: str) -> str:
    """把 ``\\(...\\)`` / ``\\[...\\]`` 统一成 Obsidian 标准数学分隔符 ``$...$`` / ``$$...$$``。

    模型经常用 LaTeX 原生的 ``\\( \\)`` 行内分隔符，虽然多数渲染器也认，但
    Obsidian 官方推荐的是 ``$`` 系，统一后更稳、也更清爽。
    """
    t = text.replace("\\[", "$$").replace("\\]", "$$")
    t = t.replace("\\(", "$").replace("\\)", "$")
    return t


# --------------------------------------------------------------------------
# 命令行自测：python zhipu_vl.py <图片路径>
# --------------------------------------------------------------------------
if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("用法: python zhipu_vl.py <图片路径> [rotate=auto|0|90|180|270]")
        raise SystemExit(2)
    path = Path(sys.argv[1])
    rot = sys.argv[2] if len(sys.argv) > 2 else None
    print("配置:", describe())
    out = transcribe(path.read_bytes(), rotate=rot)
    print("=" * 60)
    print(out)
