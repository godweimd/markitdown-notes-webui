# -*- coding: utf-8 -*-
"""扫描件 OCR 回退模块（纯本地离线）。

MarkItDown 的标准转换器只能读取文档里的**文字层**。扫描件 PDF、截图、照片
这类文件里全是像素，没有文字层，转换结果就是空的。这个模块负责兜底：

1. 用 PyMuPDF 把 PDF 每页渲染成位图（图片文件则直接使用原图）
2. 交给识别引擎做中英文识别（两种后端，见下）
3. 按版面位置重排文字，合并成有段落结构的 Markdown

识别后端可按请求切换（``ocr_file(..., engine=...)``，对应界面上的引擎下拉框）：
* ``auto``（默认）—— 云端优先，失败自动回落本地，不会让整次转换失败。
* ``local`` —— 只用 **RapidOCR 本地离线**：不需要联网和任何 Key，速度略慢。
* ``cloud`` —— 只用 **腾讯云 OCR**（``tencent_config.json`` 里填好 SecretId/SecretKey
  后启用）：精度更高，适合手写笔记和低质量扫描件，但需要联网、按次计费；
  失败时**不**回落本地（错误原因记录在 ``engine_info()["cloud_error"]``）。
* ``vl`` —— 只用 **智谱 GLM 视觉理解**（``zhipu_config.json`` 里填好 api_key 后启用）：
  多模态大模型整页“看懂”内容，把数学公式转成 **LaTeX 代码**（``$...$`` / ``$$...$$``），
  手写笔记 + 公式页首选；需要联网、按次计费。
"""

from __future__ import annotations

import io
import os
import re
import statistics
import sys
import threading
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

# 同目录模块：腾讯云 OCR（可选，未配置密钥时自动退回本地引擎）
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
try:
    import tencent_ocr  # type: ignore
except Exception:  # noqa: BLE001  —— 缺这个模块也不该影响本地 OCR
    tencent_ocr = None  # type: ignore

# 同目录模块：智谱 GLM 视觉理解（可选，未配置 Key 时不可选）
try:
    import zhipu_vl  # type: ignore
except Exception:  # noqa: BLE001
    zhipu_vl = None  # type: ignore

# 页面渲染倍率。1 倍 ≈ 72 DPI。
# 实测：2 倍对低分辨率扫描件偏糊，4 倍反而会让密集的小字（如键位字母）退化，
# 3 倍（≈216 DPI）在这两者之间效果最好。
RENDER_SCALE = 3.0
# 单次最多处理多少页，避免误传超大文件时卡死
MAX_PAGES = 60
# 低于该置信度的识别结果直接丢弃
MIN_SCORE = 0.5

# ---- 混合模式（保留插图）相关 ----
# 图片面积占页面的比例达到这个值，就怀疑它是“整页扫描件”而不是插图。
WHOLE_PAGE_RATIO = 0.5
# 但要同时满足“页面别处几乎没有矢量笔迹”才算扫描件 ——
# 手写批注页上的图可能也很大，那种应该保留图片、只把手写识别成文字。
MIN_OUTSIDE_STROKES = 20
# 小于该边长（pt）的图通常是图标、分隔线、装饰块，不作为插图导出。
MIN_IMAGE_SIDE = 30.0
# JPEG 转 PNG 后的体积上限，超过则保留原始 JPEG，避免输出文件爆炸。
MAX_PNG_BYTES = 1_500_000
# 图片在 Markdown 中的相对目录
ASSET_DIR = "assets"
# Markdown 里引用插图的写法：
#   "wiki" → ![[图片名]]               Obsidian 原生语法，挪动位置也不会断链
#   "md"   → ![说明](assets/xx.png)    标准 Markdown 相对路径，跨软件通用
# 选 "wiki" 时图片名会自动带上文档名前缀（如 键盘盲打_page1_img1.png）：
# Obsidian 是按文件名在**整个仓库**里找图的，不同文档导出的 page1_img1.png
# 会互相串，加前缀才能保证唯一。
LINK_STYLE = "wiki"

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".webp", ".gif"}
PDF_EXTS = {".pdf"}

# ---- OCR 引擎模式 ----
# auto  = 云端优先，失败回落本地（默认）
# local = 只用本地 RapidOCR
# cloud = 只用腾讯云，失败不回落本地
# vl    = 只用智谱 GLM 视觉理解：整页理解，公式转 LaTeX（手写笔记首选）
ENGINE_MODES = ("auto", "local", "cloud", "vl")

_engine: Optional[Any] = None
_engine_error: Optional[str] = None
_engine_lock = threading.Lock()

# 最近一次转换实际用到的引擎（"tencent" / "local" / None），以及云端失败原因
_last_engine: Optional[str] = None
_cloud_error: Optional[str] = None


# --------------------------------------------------------------------------
# 引擎
# --------------------------------------------------------------------------
def _cloud_enabled() -> bool:
    """腾讯云 OCR 是否已配置好并且开启。"""
    if tencent_ocr is None:
        return False
    try:
        return tencent_ocr.is_enabled()
    except Exception:  # noqa: BLE001
        return False


def _vl_enabled() -> bool:
    """智谱 GLM 视觉理解是否已配置好并且开启。"""
    if zhipu_vl is None:
        return False
    try:
        return zhipu_vl.is_enabled()
    except Exception:  # noqa: BLE001
        return False


def _local_ready() -> bool:
    """本地 OCR 依赖是否就绪 —— 只查模块是否存在，不加载模型，很快。"""
    return ocr_installed()


def normalize_mode(engine: Optional[str]) -> str:
    """把外部传入的引擎名收敛到 ENGINE_MODES 之一，非法值退回 ``auto``。"""
    return engine if engine in ENGINE_MODES else "auto"


def mode_ready(mode: str) -> bool:
    """该模式当前是否有可用后端（不加载模型）。"""
    mode = normalize_mode(mode)
    if mode == "local":
        return _local_ready()
    if mode == "cloud":
        return _cloud_enabled()
    if mode == "vl":
        return _vl_enabled()
    return _local_ready() or _cloud_enabled()


def active_engine() -> str:
    """当前会优先使用的引擎：'tencent' / 'local' / 'none'。"""
    if _cloud_enabled():
        return "tencent"
    if ocr_installed():
        return "local"
    return "none"


def last_engine() -> Optional[str]:
    """本次转换实际用到的引擎（云端失败回落时会变成 'local'）。"""
    return _last_engine


def engine_info() -> Dict[str, Any]:
    """给 /api/info 用的完整引擎信息。"""
    info: Dict[str, Any] = {
        "engine": active_engine(),
        "last": _last_engine,
        "cloud_error": _cloud_error,
        # 各模式是否可用（供界面禁用无效选项）
        "available": {
            "auto": mode_ready("auto"),
            "local": mode_ready("local"),
            "cloud": mode_ready("cloud"),
            "vl": mode_ready("vl"),
        },
    }
    if tencent_ocr is not None:
        try:
            info["tencent"] = tencent_ocr.describe()
        except Exception as exc:  # noqa: BLE001
            info["tencent"] = {"state": "error", "error": f"{type(exc).__name__}: {exc}"}
    else:
        info["tencent"] = {"state": "unavailable"}
    if zhipu_vl is not None:
        try:
            info["zhipu"] = zhipu_vl.describe()
        except Exception as exc:  # noqa: BLE001
            info["zhipu"] = {"state": "error", "error": f"{type(exc).__name__}: {exc}"}
    else:
        info["zhipu"] = {"state": "unavailable"}
    return info


def _get_engine() -> Optional[Any]:
    """惰性创建 RapidOCR 引擎（首次约 1~3 秒，之后复用）。"""
    global _engine, _engine_error
    if _engine is not None or _engine_error is not None:
        return _engine

    with _engine_lock:
        if _engine is not None or _engine_error is not None:
            return _engine
        try:
            from rapidocr_onnxruntime import RapidOCR  # type: ignore

            _engine = RapidOCR()
        except Exception as exc:  # noqa: BLE001
            _engine_error = f"{type(exc).__name__}: {exc}"
            _engine = None
        return _engine


def ocr_available() -> bool:
    """OCR 是否可用。云端模式只看密钥，本地模式会加载模型（较慢）。"""
    if _cloud_enabled():
        return True
    return _get_engine() is not None


def ocr_installed() -> bool:
    """所需依赖是否具备 —— 只做模块查找，不加载模型，很快。"""
    import importlib.util

    def has(mod: str) -> bool:
        try:
            return importlib.util.find_spec(mod) is not None
        except (ImportError, ValueError):
            return False

    pdf_ready = has("pymupdf") or has("fitz")
    # 云端引擎靠接口识别，不需要 rapidocr 和 numpy，只要能把 PDF 渲染出来
    if _cloud_enabled():
        return pdf_ready and has("PIL")
    return has("rapidocr_onnxruntime") and pdf_ready and has("numpy") and has("PIL")


def _import_fitz():
    """PyMuPDF 新版叫 pymupdf，旧版叫 fitz，两者都兼容。"""
    try:
        import pymupdf  # type: ignore

        return pymupdf
    except ImportError:
        import fitz  # type: ignore

        return fitz


def ocr_status() -> str:
    """给前端展示的状态描述。"""
    if _cloud_enabled():
        return "tencent"
    if not ocr_installed():
        return "missing"
    if ocr_available():
        return "ready"
    return _engine_error or "error"


def is_ocr_candidate(filename: str) -> bool:
    """这个文件是否值得尝试 OCR。"""
    if not filename:
        return False
    ext = ("." + filename.rsplit(".", 1)[-1].lower()) if "." in filename else ""
    return ext in IMAGE_EXTS or ext in PDF_EXTS


# --------------------------------------------------------------------------
# 版面重排
# --------------------------------------------------------------------------
@dataclass
class _Line:
    text: str
    x: float
    y: float
    height: float
    x1: float = 0.0
    y0: float = 0.0
    y1: float = 0.0

    @property
    def right(self) -> float:
        return self.x1 or self.x


def _is_cjk(ch: str) -> bool:
    code = ord(ch)
    return (
        0x4E00 <= code <= 0x9FFF
        or 0x3400 <= code <= 0x4DBF
        or 0x3000 <= code <= 0x303F
        or 0xFF00 <= code <= 0xFFEF
    )


def _join_tokens(tokens: List[str]) -> str:
    """中文之间不加空格，英文/数字之间加空格。"""
    out = ""
    for tk in tokens:
        tk = tk.strip()
        if not tk:
            continue
        if not out:
            out = tk
            continue
        prev = out[-1]
        nxt = tk[0]
        if _is_cjk(prev) or _is_cjk(nxt) or prev in "，。；：、！？）】》」』%":
            out += tk
        elif prev in "（【《「『":
            out += tk
        else:
            out += " " + tk
    return out


def _layout_to_markdown(items: List[Tuple[List[List[float]], str, float]]) -> str:
    """把 OCR 的零散文本框按版面重排成段落。"""
    lines: List[_Line] = []
    for box, text, score in items:
        text = (text or "").strip()
        if not text or score < MIN_SCORE:
            continue
        xs = [p[0] for p in box]
        ys = [p[1] for p in box]
        lines.append(
            _Line(
                text=text,
                x=min(xs),
                y=sum(ys) / len(ys),
                height=max(ys) - min(ys),
            )
        )

    if not lines:
        return ""

    lines.sort(key=lambda ln: ln.y)
    heights = [ln.height for ln in lines if ln.height > 0]
    med_h = statistics.median(heights) if heights else 12.0

    # 合并到同一行
    rows: List[List[_Line]] = []
    for ln in lines:
        if rows and abs(ln.y - rows[-1][0].y) <= med_h * 0.6:
            rows[-1].append(ln)
        else:
            rows.append([ln])

    # 行内按 x 排序；行间按纵向间距判断段落
    paragraphs: List[str] = []
    buf: List[str] = []
    prev_y: Optional[float] = None

    for row in rows:
        row.sort(key=lambda ln: ln.x)
        text = _join_tokens([ln.text for ln in row])
        y = sum(ln.y for ln in row) / len(row)

        if prev_y is not None and (y - prev_y) > med_h * 1.9:
            if buf:
                paragraphs.append("\n".join(buf))
                buf = []
        buf.append(text)
        prev_y = y

    if buf:
        paragraphs.append("\n".join(buf))

    return "\n\n".join(p for p in paragraphs if p.strip())


# --------------------------------------------------------------------------
# 识别
# --------------------------------------------------------------------------
def _to_bgr(png_bytes: bytes):
    """PNG/JPEG 字节 → BGR ndarray（RapidOCR 需要 OpenCV 风格输入）。"""
    import numpy as np
    from PIL import Image

    img = Image.open(io.BytesIO(png_bytes)).convert("RGB")
    return np.array(img)[:, :, ::-1]


def _run_engine(
    image_bytes: bytes, engine: str = "auto", action: Optional[str] = None
) -> List[Tuple[List[List[float]], str, float]]:
    """选一个后端做识别，返回统一的 ``(box, text, score)`` 列表。

    Args:
        engine: ``"auto"`` 云端优先失败回落本地 / ``"local"`` 仅本地 /
            ``"cloud"`` 仅云端（失败**不**回落）。非法值按 ``auto`` 处理。
        action: 仅对云端生效，覆盖腾讯云接口名（如 ``GeneralHandwritingOCR``
            手写体识别）。为 None 时沿用 tencent_config.json 里的配置。
    """
    global _last_engine, _cloud_error
    mode = normalize_mode(engine)

    # 仅本地：不碰网络，也不清除历史错误记录（避免误报）
    if mode == "local":
        _cloud_error = None
        _last_engine = "local"
        return _run_local(image_bytes)

    # 仅云端：不回落，失败就返回空（原因留给 _cloud_error 上报）
    if mode == "cloud":
        if not _cloud_enabled():
            _cloud_error = "已选择「仅云端」，但云端未启用：请检查 tencent_config.json 的 enabled 与密钥"
            _last_engine = "tencent"
            return []
        _last_engine = "tencent"
        items = _run_cloud(image_bytes, action=action)
        return items if items is not None else []

    # auto：云端优先，失败（网络/欠费/密钥失效）回落本地，不让整次转换失败
    if _cloud_enabled():
        items = _run_cloud(image_bytes, action=action)
        if items is not None:
            _last_engine = "tencent"
            return items
    else:
        _cloud_error = None
    _last_engine = "local"
    return _run_local(image_bytes)


def _run_cloud(
    image_bytes: bytes, action: Optional[str] = None
) -> Optional[List[Tuple[List[List[float]], str, float]]]:
    """调用腾讯云 OCR；失败返回 None，原因记录在 ``_cloud_error``。"""
    global _cloud_error
    if tencent_ocr is None:
        _cloud_error = "tencent_ocr 模块不可用"
        return None
    try:
        if action:
            cfg = tencent_ocr.load_config()
            cfg["action"] = action
            items = tencent_ocr.recognize(image_bytes, config=cfg)
        else:
            items = tencent_ocr.recognize(image_bytes)
        _cloud_error = None
        return items
    except Exception as exc:  # noqa: BLE001
        _cloud_error = f"{type(exc).__name__}: {exc}"
        return None


def _transcribe_vl(image_bytes: bytes, model: Optional[str] = None) -> str:
    """智谱 GLM 视觉理解：整页转写成带 LaTeX 的 Markdown。

    与 ``_run_engine`` 不同，VL 模型返回的是**整页文本**而不是文字块，
    所以不进 ``(box, text, score)`` 那套版面重排流程。

    ``model`` 覆盖配置里的模型（前端「模型」下拉框切换用）。
    """
    global _cloud_error
    if zhipu_vl is None:
        _cloud_error = "zhipu_vl 模块不可用"
        return ""
    try:
        text = zhipu_vl.transcribe(image_bytes, model=model)
        _cloud_error = None
        return text or ""
    except Exception as exc:  # noqa: BLE001
        _cloud_error = f"{type(exc).__name__}: {exc}"
        return ""


def _ocr_vl_pdf(data: bytes, model: Optional[str] = None) -> Optional[str]:
    """vl 模式下的 PDF：逐页渲染后交给 GLM 转写，再拼起来。"""
    fitz = _import_fitz()
    pages: List[Tuple[int, str]] = []
    with fitz.open(stream=data, filetype="pdf") as doc:
        for page_no, page in enumerate(doc, 1):
            if page_no > MAX_PAGES:
                break
            text = _transcribe_vl(_render_page(page), model=model).strip()
            if text:
                pages.append((page_no, text))

    if not pages:
        return None
    if len(pages) == 1:
        return pages[0][1]
    return "\n\n".join(f"## 第 {no} 页\n\n{body}" for no, body in pages)


def _run_local(image_bytes: bytes) -> List[Tuple[List[List[float]], str, float]]:
    """本地 RapidOCR 识别。"""
    engine = _get_engine()
    if engine is None:
        return []

    result = engine(_to_bgr(image_bytes))

    # 兼容不同版本的返回结构：(result, elapse) 或 直接 result
    payload = result[0] if isinstance(result, tuple) else result
    items: List[Tuple[List[List[float]], str, float]] = []

    if not payload:
        return items

    for entry in payload:
        # 新版本: [box, text, score]；旧版本: (box, text, score)
        if len(entry) >= 3:
            box, text, score = entry[0], entry[1], entry[2]
        elif len(entry) == 2:
            box, text = entry[0], entry[1]
            score = 1.0
        else:
            continue
        try:
            items.append((box, str(text), float(score)))
        except (TypeError, ValueError):
            continue

    return items


def _render_page(page) -> bytes:
    """把 PDF 某一页渲染成 PNG 字节。"""
    fitz = _import_fitz()
    pix = page.get_pixmap(matrix=fitz.Matrix(RENDER_SCALE, RENDER_SCALE))
    return pix.tobytes("png")


# --------------------------------------------------------------------------
# 插图导出
# --------------------------------------------------------------------------
_BAD_NAME_CHARS = re.compile(r'[\\/:*?"<>|#^\[\]\x00-\x1f]')


def _safe_stem(name: str) -> str:
    """把名字清理成安全的文件名/链接名。

    Obsidian 的 wikilink 里 ``# ^ [ ] |`` 都有特殊含义，留在文件名里会让
    链接解析出错，所以和 Windows 禁用字符一起去掉。
    """
    cleaned = _BAD_NAME_CHARS.sub("_", name or "").strip(" .")
    return cleaned[:60]


def _doc_stem(filename: str) -> str:
    """从 "C:/a/b/键盘盲打.pdf" 取出 "键盘盲打"。"""
    name = re.split(r"[\\/]", filename or "")[-1]
    return name.rsplit(".", 1)[0] if "." in name else name


class AssetSink:
    """收集本次转换导出的图片资源（保持插入顺序，名字去重）。

    prefix 用于 wikilink 模式：Obsidian 按文件名在整个仓库里找图，不同文档
    导出的 page1_img1.png 会重名串味，所以用文档名做前缀保证全仓库唯一。
    """

    def __init__(self, prefix: str = "") -> None:
        self.items: List[Dict[str, Any]] = []
        self._used: set = set()
        safe = _safe_stem(prefix)
        self.prefix = f"{safe}_" if safe else ""

    def add(self, data: bytes, name: str, mime: str) -> str:
        stem, _, ext = name.rpartition(".")
        if not stem:
            stem, ext = name, "png"
        final, i = f"{self.prefix}{name}", 1
        while final in self._used:
            i += 1
            final = f"{self.prefix}{stem}_{i}.{ext}"
        self._used.add(final)
        self.items.append(
            {"name": final, "mime": mime, "data": data, "size": len(data)}
        )
        return final


def _to_png(data: bytes) -> Optional[bytes]:
    """把任意位图字节转成 PNG。"""
    try:
        from PIL import Image

        img = Image.open(io.BytesIO(data))
        if img.mode not in ("RGB", "RGBA", "L"):
            img = img.convert("RGB")
        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=True)
        return buf.getvalue()
    except Exception:  # noqa: BLE001
        return None


def _encode_image(data: bytes, src_ext: str) -> Tuple[bytes, str]:
    """决定导出的字节与 MIME：优先 PNG，JPEG 转过大的话退回原格式。"""
    if src_ext == "png":
        return data, "image/png"

    png = _to_png(data)
    if png is not None and (src_ext not in ("jpg", "jpeg") or len(png) <= MAX_PNG_BYTES):
        return png, "image/png"
    if src_ext in ("jpg", "jpeg"):
        return data, "image/jpeg"
    if png is not None:
        return png, "image/png"
    return data, "application/octet-stream"


def _extract_xref_image(doc, xref: int) -> Tuple[Optional[bytes], str]:
    """按 xref 取出 PDF 内嵌图片的原始字节。"""
    try:
        raw = doc.extract_image(xref)
    except Exception:  # noqa: BLE001
        return None, ""
    if not raw or not raw.get("image"):
        return None, ""
    return _encode_image(raw["image"], str(raw.get("ext") or "").lower())


def _render_region(page, bbox) -> Tuple[Optional[bytes], str]:
    """兜底：直接把页面上的某块区域渲染成 PNG（图片被裁剪/拼贴时用）。"""
    fitz = _import_fitz()
    try:
        pix = page.get_pixmap(matrix=fitz.Matrix(RENDER_SCALE, RENDER_SCALE), clip=bbox)
        return pix.tobytes("png"), "image/png"
    except Exception:  # noqa: BLE001
        return None, ""


def _outside_stroke_count(page, bbox) -> int:
    """统计该区域之外的矢量笔迹数量（手写、批注都由大量短路径构成）。"""
    count = 0
    try:
        for item in page.get_drawings():
            rect = item.get("rect")
            if rect is None:
                count += 1
            else:
                cx = (rect.x0 + rect.x1) / 2
                cy = (rect.y0 + rect.y1) / 2
                if not (bbox.x0 <= cx <= bbox.x1 and bbox.y0 <= cy <= bbox.y1):
                    count += 1
            if count >= MIN_OUTSIDE_STROKES:
                return count
    except Exception:  # noqa: BLE001
        return 0
    return count


def _is_scan_page(page, bbox) -> bool:
    """判断这张大图是不是“整页扫描件”。

    面积大 **且** 页面别处几乎没有矢量笔迹 => 这一页本身就是一张扫描图，
    应当整页 OCR 成文字；否则（例如手写批注页里的大插图）仍按插图保留。
    """
    rect = page.rect
    page_area = abs(rect.width * rect.height) or 1.0
    if bbox.get_area() / page_area < WHOLE_PAGE_RATIO:
        return False
    return _outside_stroke_count(page, bbox) < MIN_OUTSIDE_STROKES


def _collect_page_images(page, sink: AssetSink, page_no: int) -> List[Tuple[Any, str]]:
    """导出页面上真正的“插图”，返回 [(bbox, 资源名)]。"""
    fitz = _import_fitz()
    doc = page.parent

    try:
        infos = page.get_image_info(xrefs=True)
    except Exception:  # noqa: BLE001
        infos = []

    found: List[Tuple[Any, str]] = []
    seen: set = set()
    index = 0

    for info in infos:
        try:
            bbox = fitz.Rect(info["bbox"])
        except Exception:  # noqa: BLE001
            continue
        if bbox.width <= 1 or bbox.height <= 1:
            continue
        if _is_scan_page(page, bbox):
            continue
        if min(bbox.width, bbox.height) < MIN_IMAGE_SIDE:
            continue

        key = (
            round(bbox.x0, 1),
            round(bbox.y0, 1),
            round(bbox.x1, 1),
            round(bbox.y1, 1),
        )
        if key in seen:
            continue
        seen.add(key)

        xref = int(info.get("xref") or 0)
        data: Optional[bytes] = None
        mime = ""
        if xref > 0:
            data, mime = _extract_xref_image(doc, xref)
        if not data:
            data, mime = _render_region(page, bbox)
        if not data:
            continue

        ext = {"image/png": "png", "image/jpeg": "jpg", "image/gif": "gif"}.get(mime, "png")
        index += 1
        name = sink.add(data, f"page{page_no}_img{index}.{ext}", mime)
        found.append((bbox, name))

    return found


# --------------------------------------------------------------------------
# 混合排版：插图 + 文字
# --------------------------------------------------------------------------
def _drop_inside_boxes(
    items: List[Tuple[List[List[float]], str, float]],
    rects: List[Any],
    scale: float,
) -> List[Tuple[List[List[float]], str, float]]:
    """丢弃落在插图区域里的识别结果。

    这些文字本来就属于那张插图（已经原样保留成图片了），
    再 OCR 一遍只会得到重复且更差的文本。
    """
    if not rects:
        return items

    kept = []
    for box, text, score in items:
        xs = [p[0] / scale for p in box]
        ys = [p[1] / scale for p in box]
        cx = (min(xs) + max(xs)) / 2
        cy = (min(ys) + max(ys)) / 2
        if any(r.x0 <= cx <= r.x1 and r.y0 <= cy <= r.y1 for r in rects):
            continue
        kept.append((box, text, score))
    return kept


def _items_to_blocks(
    items: List[Tuple[List[List[float]], str, float]], scale: float
) -> List[Dict[str, Any]]:
    """OCR 结果 → 带坐标的文字块（供与插图一起排序）。"""
    lines: List[_Line] = []
    for box, text, score in items:
        text = (text or "").strip()
        if not text or score < MIN_SCORE:
            continue
        xs = [p[0] / scale for p in box]
        ys = [p[1] / scale for p in box]
        lines.append(
            _Line(
                text=text,
                x=min(xs),
                y=sum(ys) / len(ys),
                height=max(ys) - min(ys),
                x1=max(xs),
                y0=min(ys),
                y1=max(ys),
            )
        )

    if not lines:
        return []

    lines.sort(key=lambda ln: ln.y)
    heights = [ln.height for ln in lines if ln.height > 0]
    med_h = statistics.median(heights) if heights else 12.0

    rows: List[List[_Line]] = []
    for ln in lines:
        if rows and abs(ln.y - rows[-1][0].y) <= med_h * 0.6:
            rows[-1].append(ln)
        else:
            rows.append([ln])

    blocks: List[Dict[str, Any]] = []
    current: Optional[Dict[str, Any]] = None
    prev_y: Optional[float] = None

    for row in rows:
        row.sort(key=lambda ln: ln.x)
        text = _join_tokens([ln.text for ln in row])
        y = sum(ln.y for ln in row) / len(row)
        y0 = min(ln.y0 for ln in row)
        y1 = max(ln.y1 for ln in row)
        x0 = min(ln.x for ln in row)

        if current is None or prev_y is None or (y - prev_y) > med_h * 1.9:
            current = {
                "kind": "text",
                "text": text,
                "y0": y0,
                "y1": y1,
                "x0": x0,
            }
            blocks.append(current)
        else:
            current["text"] += "\n" + text
            current["y1"] = y1
        prev_y = y

    return blocks


def _page_blocks(
    page, sink: AssetSink, page_no: int, keep_images: bool, engine: str = "auto"
) -> List[Dict[str, Any]]:
    """把一页拆成“插图块 + 文字块”，按纵坐标排序。"""
    blocks: List[Dict[str, Any]] = []
    rects: List[Any] = []

    if keep_images:
        for bbox, name in _collect_page_images(page, sink, page_no):
            rects.append(bbox)
            blocks.append(
                {
                    "kind": "image",
                    "name": name,
                    "alt": f"第 {page_no} 页插图",
                    "y0": bbox.y0,
                    "y1": bbox.y1,
                    "x0": bbox.x0,
                }
            )

    items = _run_engine(_render_page(page), engine)
    items = _drop_inside_boxes(items, rects, RENDER_SCALE)
    blocks.extend(_items_to_blocks(items, RENDER_SCALE))

    blocks.sort(key=lambda b: (round(float(b.get("y0") or 0), 1), round(float(b.get("x0") or 0), 1)))
    return blocks


def _blocks_to_markdown(blocks: List[Dict[str, Any]]) -> str:
    parts: List[str] = []
    for block in blocks:
        if block["kind"] == "image":
            name = block["name"]
            if LINK_STYLE == "wiki":
                # Obsidian 原生嵌入语法：图片名在仓库里唯一，挪位置也不会断链
                parts.append(f"![[{name}]]")
            else:
                alt = block.get("alt") or "图片"
                parts.append(f"![{alt}]({ASSET_DIR}/{name})")
        else:
            text = (block.get("text") or "").strip()
            if text:
                parts.append(text)
    return "\n\n".join(parts)


# --------------------------------------------------------------------------
# 对外入口
# --------------------------------------------------------------------------
def _ocr_pdf(
    data: bytes, keep_images: bool, doc_name: str = "", engine: str = "auto"
) -> Tuple[Optional[str], List[Dict[str, Any]]]:
    fitz = _import_fitz()
    # wikilink 模式下给图片名加文档名前缀，避免不同文档的同名图互相串
    sink = AssetSink(prefix=doc_name if LINK_STYLE == "wiki" else "")
    pages: List[Tuple[int, str]] = []
    total = 0

    with fitz.open(stream=data, filetype="pdf") as doc:
        for page_no, page in enumerate(doc, 1):
            if page_no > MAX_PAGES:
                break
            total += 1
            body = _blocks_to_markdown(
                _page_blocks(page, sink, page_no, keep_images, engine)
            )
            if body.strip():
                pages.append((page_no, body))

    if not pages:
        return None, sink.items

    if len(pages) == 1:
        markdown = pages[0][1]
    else:
        markdown = "\n\n".join(f"## 第 {no} 页\n\n{body}" for no, body in pages)

    return markdown, sink.items


def ocr_file(
    data: bytes,
    filename: str,
    keep_images: bool = True,
    engine: str = "auto",
    action: Optional[str] = None,
    model: Optional[str] = None,
) -> Tuple[Optional[str], List[Dict[str, Any]]]:
    """对没有文字层的文件做识别。

    Args:
        data: 文件原始字节
        filename: 原始文件名（用于判断类型）
        keep_images: 是否把文档里的插图导出成图片并在 Markdown 中引用，
            同时只对插图之外的区域做 OCR（适合“手写 + 截图”的笔记页）
        engine: OCR 引擎选择 —— ``auto`` / ``local`` / ``cloud`` / ``vl``（见模块开头说明）
        action: 仅对云端生效，覆盖腾讯云接口名（如 ``GeneralHandwritingOCR``）
        model: 仅对 vl 模式生效，覆盖智谱模型（如 ``glm-4.5v``）；为 None 用配置值

    Returns:
        (Markdown 文本, 导出的图片资源列表)；不支持或识别不出内容时文本为 None。
    """
    global _last_engine, _cloud_error
    _last_engine = None
    _cloud_error = None

    if not data or not is_ocr_candidate(filename):
        return None, []

    mode = normalize_mode(engine)
    if not mode_ready(mode):
        # 所选模式没有可用后端，把原因记好交给上层提示
        if mode == "cloud":
            _cloud_error = "已选择「仅云端」，但云端 OCR 未启用（请检查 tencent_config.json）"
        elif mode == "vl":
            _cloud_error = "已选择「智谱 GLM 视觉理解」，但未配置 API Key（请检查 zhipu_config.json）"
        return None, []

    ext = "." + filename.rsplit(".", 1)[-1].lower()

    # vl 模式：整页交给 GLM 转写，直接产出带 LaTeX 的 Markdown（不做版面重排）
    if mode == "vl":
        _last_engine = "zhipu"
        try:
            if ext in PDF_EXTS:
                return _ocr_vl_pdf(data, model=model), []
            return (_transcribe_vl(data, model=model) or None), []
        except Exception:  # noqa: BLE001
            return None, []

    try:
        if ext in PDF_EXTS:
            return _ocr_pdf(data, keep_images, _doc_stem(filename), mode)

        markdown = _layout_to_markdown(_run_engine(data, mode, action=action))
        return (markdown or None), []
    except Exception:  # noqa: BLE001
        # OCR 失败不应该让整个转换请求崩掉，交给上层决定如何提示
        return None, []
